"""Test wiring: in-memory Qdrant, fake Redis, local hash embedder. No network needed."""
import pytest
from fakeredis import aioredis
from httpx import ASGITransport, AsyncClient
from qdrant_client import AsyncQdrantClient

from app.config import Settings
from app.embedder import HashEmbedder
from app.main import build_service, create_app

ADMIN = {"Authorization": "Bearer admin-test"}
READ = {"Authorization": "Bearer read-test"}
DIM = 64


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, embed_mode="hash", embed_dim=DIM, api_admin_token="admin-test",
                    api_read_token="read-test", chunk_max_tokens=120, table_max_tokens=150, max_upload_mb=1)


@pytest.fixture
async def service(settings):
    svc = await build_service(settings, qdrant=AsyncQdrantClient(location=":memory:"),
                              redis=aioredis.FakeRedis(), embedder=HashEmbedder(DIM))
    yield svc
    await svc.store.q.close()


@pytest.fixture
async def client(settings, service):
    app = create_app(settings, service)
    app.state.svc = service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def md_tc(gift_line: str = "Every successful claim receives an R500 voucher.", extra: str = "") -> bytes:
    return f"""# Promo Terms and Conditions

## Promotion period
The promotion is valid for purchases made between 1 September 2026 and 30 November 2026.

## Participating retailers
Purchases must be made at MTN Stores, Vodacom Shops or Takealot.

## Gifts
Each valid claim receives the gift listed for the purchased device:

| Device | Gift |
|---|---|
| Galaxy A55 | R500 Takealot voucher |
| Galaxy S25 | Samsung Galaxy Buds3 |

{gift_line}
{extra}
""".encode()


async def upload(client, campaign_id: int, data: bytes, name: str = "tc.md", **form):
    return await client.post(f"/tc/{campaign_id}/upload", headers=ADMIN,
                             files={"file": (name, data)}, data={k: str(v).lower() for k, v in form.items()})
