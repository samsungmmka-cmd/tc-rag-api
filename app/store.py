"""Qdrant storage.

Two collections:
  tc_chunks    one point per chunk. Payload: campaign_id, tc_version, chunk_index, section, text...
               Indexed on campaign_id and tc_version; every read filters on both.
  tc_registry  one point per campaign (point id = campaign_id). Holds the ACTIVE tc_version
               and upload metadata. Reads go through this pointer, so a half-written new
               version is never visible.
"""
import logging
import uuid
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from app.config import Settings

log = logging.getLogger(__name__)

_NS = uuid.UUID("5b7c2a1e-2f0a-4c55-9a39-6f0b6c1d7e21")
_BATCH = 128


class StoreError(RuntimeError):
    """Vector DB problem. Maps to HTTP 503."""


def point_id(campaign_id: int, version: int, index: int) -> str:
    """Deterministic: a retried write overwrites the same points instead of duplicating them."""
    return str(uuid.uuid5(_NS, f"{campaign_id}:{version}:{index}"))


def _match(key: str, value: int) -> models.FieldCondition:
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def _filter(campaign_id: int, version: int | None = None, exclude_version: int | None = None) -> models.Filter:
    must = [_match("campaign_id", campaign_id)]
    if version is not None:
        must.append(_match("tc_version", version))
    must_not = [_match("tc_version", exclude_version)] if exclude_version is not None else None
    return models.Filter(must=must, must_not=must_not)


class Store:
    def __init__(self, client: AsyncQdrantClient, s: Settings):
        self.q = client
        self.chunks_col = s.qdrant_collection
        self.reg_col = s.qdrant_registry_collection
        self.dim = s.embed_dim
        self._reg_vector: Any = {}

    # ---------------------------------------------------------------- setup
    async def ensure(self) -> None:
        """Create collections and indexes if missing; refuse to start on a dimension mismatch."""
        if not await self.q.collection_exists(self.chunks_col):
            await self.q.create_collection(
                self.chunks_col,
                vectors_config=models.VectorParams(size=self.dim, distance=models.Distance.COSINE),
            )
            log.info("created collection", extra={"data": {"collection": self.chunks_col, "dim": self.dim}})
        info = await self.q.get_collection(self.chunks_col)
        vectors = info.config.params.vectors
        size = getattr(vectors, "size", None)
        if size != self.dim:
            raise StoreError(
                f"collection '{self.chunks_col}' has vector size {size}, EMBED_DIM is {self.dim}. "
                "Use a new QDRANT_COLLECTION for a different embedding model, then re-upload the TCs.")
        existing = set((info.payload_schema or {}).keys())
        for field in ("campaign_id", "tc_version"):
            if field not in existing:
                await self.q.create_payload_index(
                    self.chunks_col, field_name=field,
                    field_schema=models.IntegerIndexParams(type=models.IntegerIndexType.INTEGER,
                                                           lookup=True, range=False),
                    wait=True,
                )

        if not await self.q.collection_exists(self.reg_col):
            try:
                await self.q.create_collection(self.reg_col, vectors_config={})
            except Exception:  # older servers want a vector; a 1-d placeholder is fine
                await self.q.create_collection(
                    self.reg_col, vectors_config=models.VectorParams(size=1, distance=models.Distance.DOT))
        reg = await self.q.get_collection(self.reg_col)
        self._reg_vector = [1.0] if getattr(reg.config.params.vectors, "size", None) == 1 else {}

    async def ping(self) -> bool:
        await self.q.get_collections()
        return True

    # ---------------------------------------------------------------- chunks
    async def upsert_chunks(self, campaign_id: int, version: int, payloads: list[dict],
                            vectors: list[list[float]]) -> None:
        points = [
            models.PointStruct(id=point_id(campaign_id, version, p["chunk_index"]), vector=v, payload=p)
            for p, v in zip(payloads, vectors)
        ]
        for i in range(0, len(points), _BATCH):
            await self.q.upsert(self.chunks_col, points=points[i:i + _BATCH], wait=True)

    async def count(self, campaign_id: int, version: int | None = None) -> int:
        res = await self.q.count(self.chunks_col, count_filter=_filter(campaign_id, version), exact=True)
        return res.count

    async def get_chunks(self, campaign_id: int, version: int) -> list[dict]:
        out, offset = [], None
        while True:
            points, offset = await self.q.scroll(
                self.chunks_col, scroll_filter=_filter(campaign_id, version), limit=256, offset=offset,
                with_payload=True, with_vectors=False)
            out += [p.payload for p in points]
            if offset is None:
                break
        return sorted(out, key=lambda p: p.get("chunk_index", 0))

    async def search(self, campaign_id: int, version: int, vector: list[float], top_k: int) -> list[tuple[dict, float]]:
        res = await self.q.query_points(
            self.chunks_col, query=vector, query_filter=_filter(campaign_id, version),
            limit=top_k, with_payload=True, with_vectors=False)
        return [(p.payload, p.score) for p in res.points]

    async def delete_version(self, campaign_id: int, version: int) -> None:
        await self.q.delete(self.chunks_col, points_selector=models.FilterSelector(
            filter=_filter(campaign_id, version)), wait=True)

    async def delete_other_versions(self, campaign_id: int, keep_version: int | None) -> int:
        """Remove leftovers of a crashed upload (any version that is not the active one)."""
        flt = _filter(campaign_id, exclude_version=keep_version)
        n = (await self.q.count(self.chunks_col, count_filter=flt, exact=True)).count
        if n:
            await self.q.delete(self.chunks_col, points_selector=models.FilterSelector(filter=flt), wait=True)
        return n

    async def delete_campaign(self, campaign_id: int) -> int:
        return await self.delete_other_versions(campaign_id, None)

    # ---------------------------------------------------------------- registry
    async def get_registry(self, campaign_id: int) -> dict | None:
        pts = await self.q.retrieve(self.reg_col, ids=[campaign_id], with_payload=True, with_vectors=False)
        return pts[0].payload if pts else None

    async def set_registry(self, campaign_id: int, record: dict) -> None:
        await self.q.upsert(self.reg_col, wait=True, points=[
            models.PointStruct(id=campaign_id, vector=self._reg_vector, payload=record)])

    async def delete_registry(self, campaign_id: int) -> None:
        await self.q.delete(self.reg_col, points_selector=models.PointIdsList(points=[campaign_id]), wait=True)

    async def list_registry(self) -> list[dict]:
        out, offset = [], None
        while True:
            points, offset = await self.q.scroll(self.reg_col, limit=256, offset=offset,
                                                 with_payload=True, with_vectors=False)
            out += [p.payload for p in points]
            if offset is None:
                break
        return sorted(out, key=lambda r: r.get("campaign_id", 0))
