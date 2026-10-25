"""TC ingestion and reads.

Replacing a campaign's TC (all under the campaign lock):
  1. read + chunk the file              (a bad file fails here; nothing is touched)
  2. same file as the active version?   -> "unchanged", unless force=true
  3. remove leftovers of a crashed upload
  4. embed every chunk, write them as version N+1
  5. verify the count of N+1 in Qdrant
  6. switch the registry pointer to N+1  <- from here claims are validated against the new TC
  7. delete version N
Any failure in 4-6 deletes the partial N+1 and keeps N active, so a campaign is never
left without rules and old and new chunks are never mixed.
"""
import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone

from starlette.concurrency import run_in_threadpool

from app.chunker import Chunk, chunk_blocks
from app.config import Settings
from app.embedder import Embedder
from app.locks import Locks
from app.readers import DocumentError, file_type, read_document
from app.schemas import (
    CampaignDetail,
    CampaignInfo,
    ChunkOut,
    ChunkPreview,
    DeleteResponse,
    RulesResponse,
    SearchChunk,
    SearchResponse,
    UploadResponse,
)
from app.store import Store, StoreError

log = logging.getLogger(__name__)


class NotFound(LookupError):
    """Maps to HTTP 404."""


class ModelMismatch(RuntimeError):
    """Campaign was embedded with another model; search would compare unrelated vectors. HTTP 409."""


def _sections(chunks: list[Chunk]) -> list[str]:
    seen: list[str] = []
    for c in chunks:
        if c.section not in seen:
            seen.append(c.section)
    return seen


def _preview(c: Chunk | dict, full: bool = False) -> ChunkPreview:
    d = c if isinstance(c, dict) else {
        "chunk_index": c.index, "section": c.section, "kind": c.kind, "tokens": c.tokens,
        "page": c.page, "part": c.part, "text": c.text}
    text = d["text"]
    return ChunkPreview(chunk_index=d["chunk_index"], section=d["section"], kind=d["kind"],
                        tokens=d.get("tokens", 0), page=d.get("page"), part=d.get("part"),
                        preview=text if full or len(text) <= 240 else text[:240] + "…")


def _chunk_out(p: dict) -> dict:
    return {"id": p["chunk_id"], "section": p["section"], "text": p["text"], "kind": p["kind"],
            "chunk_index": p["chunk_index"], "page": p.get("page"), "part": p.get("part")}


class TCService:
    def __init__(self, s: Settings, store: Store, embedder: Embedder, locks: Locks):
        self.s, self.store, self.embedder, self.locks = s, store, embedder, locks

    def parse(self, filename: str, data: bytes) -> tuple[list[Chunk], str | None]:
        blocks = read_document(filename, data)
        chunks, title = chunk_blocks(blocks, self.s.chunk_max_tokens, self.s.chunk_overlap_tokens,
                                     self.s.table_max_tokens)
        if not chunks:
            raise DocumentError("no text found in the document")
        return chunks, title

    # ------------------------------------------------------------------ upload
    async def ingest(self, campaign_id: int, filename: str, data: bytes, force: bool = False,
                     dry_run: bool = False) -> UploadResponse:
        t0 = time.perf_counter()
        ext = file_type(filename)
        file_hash = hashlib.sha256(data).hexdigest()
        chunks, title = await run_in_threadpool(self.parse, filename, data)
        tables = sum(c.kind == "table" for c in chunks)
        ctx = {"campaign_id": campaign_id, "file_name": filename, "chunks": len(chunks)}

        def response(status: str, version: int | None, replaced: int | None = None, **kw) -> UploadResponse:
            return UploadResponse(
                campaign_id=campaign_id, status=status, tc_version=version, replaced_version=replaced,
                file_name=filename, file_hash=file_hash, doc_title=title, chunks=len(chunks),
                table_chunks=tables, sections=_sections(chunks),
                embed_model=None if status == "dry_run" else self.embedder.model,
                duration_ms=int((time.perf_counter() - t0) * 1000), **kw)

        if dry_run:
            return response("dry_run", None, preview=[_preview(c, full=True) for c in chunks])

        async with self.locks.hold(campaign_id):
            reg = await self.store.get_registry(campaign_id)
            active = reg["tc_version"] if reg else None

            if (reg and not force and reg.get("file_hash") == file_hash
                    and reg.get("embed_model") == self.embedder.model):
                log.info("tc unchanged, skipped", extra={"data": {**ctx, "tc_version": active}})
                return response("unchanged", active)

            orphans = await self.store.delete_other_versions(campaign_id, active)
            if orphans:
                log.warning("removed leftover chunks of a failed upload",
                            extra={"data": {**ctx, "removed": orphans}})

            new_v = (active or 0) + 1
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            payloads = [{
                "campaign_id": campaign_id, "tc_version": new_v,
                "chunk_id": f"{campaign_id}-{new_v}-{c.index:04d}", "chunk_index": c.index,
                "section": c.section, "kind": c.kind, "text": c.text, "page": c.page, "part": c.part,
                "tokens": c.tokens, "file_name": filename, "doc_title": title,
                "embed_model": self.embedder.model, "created_at": now,
            } for c in chunks]

            try:
                te = time.perf_counter()
                vectors = await self.embedder.embed([c.embed_text() for c in chunks])
                embed_ms = int((time.perf_counter() - te) * 1000)
                await self.store.upsert_chunks(campaign_id, new_v, payloads, vectors)
                stored = await self.store.count(campaign_id, new_v)
                if stored != len(chunks):
                    raise StoreError(f"wrote {len(chunks)} chunks but Qdrant has {stored} for version {new_v}")
                await self.store.set_registry(campaign_id, CampaignInfo(
                    campaign_id=campaign_id, tc_version=new_v, file_name=filename, file_type=ext.lstrip("."),
                    file_size=len(data), file_hash=file_hash, doc_title=title, chunks=len(chunks),
                    table_chunks=tables, sections=_sections(chunks), embed_model=self.embedder.model,
                    embed_dim=self.embedder.dim, uploaded_at=now, previous_version=active,
                ).model_dump())
            except BaseException:
                log.error("tc upload failed, keeping the previous version",
                          extra={"data": {**ctx, "tc_version": new_v, "active_version": active}}, exc_info=True)
                try:
                    await asyncio.shield(self.store.delete_version(campaign_id, new_v))
                except Exception:   # next upload removes it (step 3)
                    log.warning("could not remove partial version", extra={"data": {**ctx, "tc_version": new_v}})
                raise

            if active is not None:
                try:
                    await self.store.delete_version(campaign_id, active)
                except Exception:   # not visible to reads; next upload removes it
                    log.warning("could not delete old version", extra={"data": {**ctx, "tc_version": active}},
                                exc_info=True)

        log.info("tc embedded", extra={"data": {**ctx, "tc_version": new_v, "replaced_version": active,
                                                "embed_ms": embed_ms}})
        return response("replaced" if active else "created", new_v, active)

    # ------------------------------------------------------------------ reads
    async def rules(self, campaign_id: int) -> RulesResponse:
        reg = await self.store.get_registry(campaign_id)
        if not reg:
            return RulesResponse(campaign_id=campaign_id, tc_version=None, chunks=[])
        payloads = await self.store.get_chunks(campaign_id, reg["tc_version"])
        if len(payloads) != reg.get("chunks"):
            log.error("registry and stored chunks disagree", extra={"data": {
                "campaign_id": campaign_id, "tc_version": reg["tc_version"],
                "expected": reg.get("chunks"), "found": len(payloads)}})
        return RulesResponse(campaign_id=campaign_id, tc_version=reg["tc_version"],
                             chunks=[ChunkOut(**_chunk_out(p)) for p in payloads])

    async def search(self, campaign_id: int, query: str, top_k: int) -> SearchResponse:
        reg = await self.store.get_registry(campaign_id)
        if not reg:
            return SearchResponse(campaign_id=campaign_id, tc_version=None, chunks=[])
        if reg.get("embed_model") != self.embedder.model or reg.get("embed_dim") != self.embedder.dim:
            raise ModelMismatch(
                f"campaign {campaign_id} was embedded with '{reg.get('embed_model')}', the service now uses "
                f"'{self.embedder.model}'. Re-upload the TC with force=true. /rules still works.")
        [vector] = await self.embedder.embed([query])
        hits = await self.store.search(campaign_id, reg["tc_version"], vector, top_k)
        return SearchResponse(campaign_id=campaign_id, tc_version=reg["tc_version"],
                              chunks=[SearchChunk(**_chunk_out(p), score=round(s, 4)) for p, s in hits])

    async def info(self, campaign_id: int) -> CampaignDetail:
        reg = await self.store.get_registry(campaign_id)
        if not reg:
            raise NotFound(f"no TC for campaign {campaign_id}")
        payloads = await self.store.get_chunks(campaign_id, reg["tc_version"])
        return CampaignDetail(
            **reg, stored_chunks=len(payloads),
            search_ready=reg.get("embed_model") == self.embedder.model and reg.get("embed_dim") == self.embedder.dim,
            chunk_preview=[_preview(p) for p in payloads])

    async def list(self) -> list[CampaignInfo]:
        return [CampaignInfo(**r) for r in await self.store.list_registry()]

    async def delete(self, campaign_id: int) -> DeleteResponse:
        async with self.locks.hold(campaign_id):
            reg = await self.store.get_registry(campaign_id)
            if reg:
                await self.store.delete_registry(campaign_id)      # reads stop first
            deleted = await self.store.delete_campaign(campaign_id)
            if not reg and not deleted:
                raise NotFound(f"no TC for campaign {campaign_id}")
        log.info("tc deleted", extra={"data": {"campaign_id": campaign_id, "deleted_chunks": deleted}})
        return DeleteResponse(campaign_id=campaign_id, deleted_version=reg["tc_version"] if reg else None,
                              deleted_chunks=deleted)
