"""TC RAG API: upload a campaign's Terms & Conditions, serve them to the validation agent.

    POST   /tc/{campaign_id}/upload   admin   file -> chunks -> embeddings -> Qdrant (replaces the old TC)
    GET    /tc/{campaign_id}/rules    read    every chunk of the active version, in document order
    POST   /tc/search                 read    similarity search inside one campaign
    GET    /tc/{campaign_id}          read    active version metadata + chunk preview
    GET    /tc                        read    all campaigns
    DELETE /tc/{campaign_id}          admin   remove a campaign's TC
    GET    /health                    open
"""
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, Path, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from qdrant_client import AsyncQdrantClient

from app.config import Settings, get_settings
from app.embedder import EmbeddingError, build_embedder
from app.locks import CampaignBusy, Locks
from app.logging_setup import setup_logging
from app.readers import DocumentError, UnsupportedFileType, file_type
from app.schemas import (
    CampaignDetail,
    CampaignInfo,
    DeleteResponse,
    RulesResponse,
    SearchRequest,
    SearchResponse,
    UploadResponse,
)
from app.service import ModelMismatch, NotFound, TCService
from app.store import Store, StoreError

log = logging.getLogger("app")
_bearer = HTTPBearer(auto_error=False)


async def build_service(s: Settings, qdrant: AsyncQdrantClient | None = None, redis=None,
                        embedder=None) -> TCService:
    """Wire the service. Tests pass an in-memory Qdrant, a fake Redis and the hash embedder."""
    if qdrant is None:
        qdrant = AsyncQdrantClient(url=s.qdrant_url, api_key=s.qdrant_api_key or None, timeout=s.qdrant_timeout)
    if redis is None and s.redis_url:
        from redis.asyncio import Redis
        redis = Redis.from_url(s.redis_url)
    store = Store(qdrant, s)
    await store.ensure()
    return TCService(s, store, embedder or build_embedder(s), Locks(redis, s.lock_ttl_seconds))


def create_app(settings: Settings | None = None, service: TCService | None = None) -> FastAPI:
    s = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(s.log_level)
        if not s.auth_enabled:
            log.warning("API_ADMIN_TOKEN and API_READ_TOKEN are empty: the API is open (dev only)")
        app.state.svc = service or await build_service(s)
        log.info("started", extra={"data": {"embed_model": app.state.svc.embedder.model,
                                            "embed_dim": app.state.svc.embedder.dim,
                                            "collection": s.qdrant_collection}})
        yield
        if service is None:
            await app.state.svc.embedder.aclose()
            await app.state.svc.store.q.close()
            if app.state.svc.locks.redis is not None:
                await app.state.svc.locks.redis.aclose()

    app = FastAPI(title="TC RAG API", version="1.0.0", lifespan=lifespan,
                  description="Embeds campaign Terms & Conditions into Qdrant for the validation agent.")

    # ------------------------------------------------------------------ auth
    def _token(cred: HTTPAuthorizationCredentials | None) -> str:
        return cred.credentials if cred and cred.scheme.lower() == "bearer" else ""

    def _is(token: str, expected: str) -> bool:
        return bool(expected) and secrets.compare_digest(token.encode(), expected.encode())

    def require_read(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
        if not s.auth_enabled:
            return
        tok = _token(cred)
        if not tok:
            raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
        if not (_is(tok, s.api_read_token) or _is(tok, s.api_admin_token)):
            raise HTTPException(403, "invalid token")

    def require_admin(cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> None:
        if not s.auth_enabled:
            return
        tok = _token(cred)
        if not tok:
            raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
        if not _is(tok, s.api_admin_token):
            raise HTTPException(403, "admin token required")

    def svc(request: Request) -> TCService:
        return request.app.state.svc

    # ------------------------------------------------------------------ errors
    def _err(status: int):
        async def handler(_: Request, exc: Exception):
            return JSONResponse(status_code=status, content={"detail": str(exc)})
        return handler

    app.add_exception_handler(UnsupportedFileType, _err(415))
    app.add_exception_handler(DocumentError, _err(422))
    app.add_exception_handler(CampaignBusy, _err(409))
    app.add_exception_handler(ModelMismatch, _err(409))
    app.add_exception_handler(NotFound, _err(404))
    app.add_exception_handler(EmbeddingError, _err(502))
    app.add_exception_handler(StoreError, _err(503))

    CampaignId = Path(gt=0, le=2**63 - 1, description="Campaign ID")

    # ------------------------------------------------------------------ routes
    @app.post("/tc/{campaign_id}/upload", response_model=UploadResponse,
              dependencies=[Depends(require_admin)],
              summary="Upload a TC file; replaces the campaign's current TC")
    async def upload(
        campaign_id: int = CampaignId,
        file: UploadFile = File(..., description=".pdf, .docx, .txt or .md (text, not scanned images)"),
        force: bool = Form(False, description="Re-embed even if the file is identical to the active one"),
        dry_run: bool = Query(False, description="Only read and chunk the file; return every chunk, store nothing"),
        service: TCService = Depends(svc),
    ):
        name = file.filename or ""
        file_type(name)                                   # 415 before reading the body
        data, limit = bytearray(), s.max_upload_bytes
        while chunk := await file.read(1024 * 1024):
            data += chunk
            if len(data) > limit:
                raise HTTPException(413, f"file larger than {s.max_upload_mb} MB")
        return await service.ingest(campaign_id, name, bytes(data), force=force, dry_run=dry_run)

    @app.get("/tc/{campaign_id}/rules", response_model=RulesResponse, dependencies=[Depends(require_read)],
             summary="All chunks of the active TC version (used by the validation agent)")
    async def rules(campaign_id: int = CampaignId, service: TCService = Depends(svc)):
        return await service.rules(campaign_id)

    @app.post("/tc/search", response_model=SearchResponse, dependencies=[Depends(require_read)],
              summary="Similarity search inside one campaign's active TC")
    async def search(body: SearchRequest, service: TCService = Depends(svc)):
        return await service.search(body.campaign_id, body.query, body.top_k)

    @app.get("/tc", response_model=list[CampaignInfo], dependencies=[Depends(require_read)],
             summary="Campaigns with an active TC")
    async def list_campaigns(service: TCService = Depends(svc)):
        return await service.list()

    @app.get("/tc/{campaign_id}", response_model=CampaignDetail, dependencies=[Depends(require_read)],
             summary="Active TC metadata and a preview of every chunk")
    async def info(campaign_id: int = CampaignId, service: TCService = Depends(svc)):
        return await service.info(campaign_id)

    @app.delete("/tc/{campaign_id}", response_model=DeleteResponse, dependencies=[Depends(require_admin)],
                summary="Delete a campaign's TC")
    async def delete(campaign_id: int = CampaignId, service: TCService = Depends(svc)):
        return await service.delete(campaign_id)

    @app.get("/health", summary="Qdrant and Redis reachability")
    async def health(service: TCService = Depends(svc)):
        checks = {}
        for name, probe in (("qdrant", service.store.ping), ("redis", service.locks.ping)):
            try:
                checks[name] = "ok" if await probe() else "down"
            except Exception as e:
                checks[name] = f"down: {e.__class__.__name__}"
        if service.locks.redis is None:
            checks["redis"] = "not configured (in-process lock)"
        ok = all(v == "ok" or v.startswith("not configured") for v in checks.values())
        return JSONResponse(status_code=200 if ok else 503, content={
            "status": "ok" if ok else "degraded", **checks,
            "embed_model": service.embedder.model, "embed_dim": service.embedder.dim})

    return app


app = create_app()
