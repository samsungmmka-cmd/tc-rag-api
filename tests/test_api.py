"""Endpoints, version replacement, rollback, locking and auth."""
import asyncio

import pytest

from app.embedder import EmbeddingError
from tests.conftest import ADMIN, READ, md_tc, upload


async def total_points(service, campaign_id: int) -> int:
    return await service.store.count(campaign_id)


async def test_upload_then_rules_in_document_order(client):
    r = await upload(client, 221, md_tc())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "created" and body["tc_version"] == 1
    assert body["doc_title"] == "Promo Terms and Conditions"
    assert body["sections"] == ["Promotion period", "Participating retailers", "Gifts"]

    rules = (await client.get("/tc/221/rules", headers=READ)).json()
    assert rules["tc_version"] == 1
    assert [c["chunk_index"] for c in rules["chunks"]] == list(range(len(rules["chunks"])))
    table = next(c for c in rules["chunks"] if c["kind"] == "table")
    assert table["section"] == "Gifts"
    assert table["text"].startswith("Each valid claim receives the gift listed")      # caption kept
    assert "| Galaxy S25 | Samsung Galaxy Buds3 |" in table["text"]


async def test_rules_contract_matches_agent(client):
    """The agent reads campaign_id, tc_version and chunks[].{id, section, text}."""
    await upload(client, 221, md_tc())
    rules = (await client.get("/tc/221/rules", headers=READ)).json()
    assert set(rules) == {"campaign_id", "tc_version", "chunks"}
    for c in rules["chunks"]:
        assert isinstance(c["id"], str) and c["section"] and c["text"]


async def test_unknown_campaign_returns_empty_rules_not_404(client):
    """Same as the agent's mock: the agent flags 'no TC rules found for campaign'."""
    r = await client.get("/tc/404/rules", headers=READ)
    assert r.status_code == 200
    assert r.json() == {"campaign_id": 404, "tc_version": None, "chunks": []}


async def test_reupload_replaces_and_deletes_old_version(client, service):
    await upload(client, 221, md_tc())
    v1_count = await total_points(service, 221)

    r = await upload(client, 221, md_tc(gift_line="Every successful claim receives an R750 voucher."))
    body = r.json()
    assert body["status"] == "replaced" and body["tc_version"] == 2 and body["replaced_version"] == 1

    rules = (await client.get("/tc/221/rules", headers=READ)).json()
    text = " ".join(c["text"] for c in rules["chunks"])
    assert rules["tc_version"] == 2 and "R750" in text and "R500 voucher" not in text
    assert await service.store.count(221, 1) == 0                       # old version gone
    assert await total_points(service, 221) == len(rules["chunks"]) == v1_count


async def test_same_file_is_not_reembedded_unless_forced(client):
    await upload(client, 221, md_tc())
    again = (await upload(client, 221, md_tc())).json()
    assert again["status"] == "unchanged" and again["tc_version"] == 1

    forced = (await upload(client, 221, md_tc(), force=True)).json()
    assert forced["status"] == "replaced" and forced["tc_version"] == 2


async def test_campaigns_are_isolated(client, service):
    await upload(client, 221, md_tc())
    await upload(client, 222, md_tc(gift_line="Every successful claim receives an R500 MTN voucher."))
    await upload(client, 221, md_tc(gift_line="changed"))
    r222 = (await client.get("/tc/222/rules", headers=READ)).json()
    assert r222["tc_version"] == 1 and "MTN voucher" in " ".join(c["text"] for c in r222["chunks"])
    assert all(c["id"].startswith("222-") for c in r222["chunks"])


async def test_embedding_failure_keeps_previous_version(client, service, monkeypatch):
    await upload(client, 221, md_tc())
    before = (await client.get("/tc/221/rules", headers=READ)).json()

    async def boom(texts):
        raise EmbeddingError("provider down")
    monkeypatch.setattr(service.embedder, "embed", boom)

    r = await upload(client, 221, md_tc(gift_line="new rules"))
    assert r.status_code == 502
    assert (await client.get("/tc/221/rules", headers=READ)).json() == before
    assert await service.store.count(221, 2) == 0


async def test_failure_after_write_removes_partial_version(client, service, monkeypatch):
    """Chunks of v2 are written, then the pointer switch fails: v2 is removed, v1 stays active."""
    await upload(client, 221, md_tc())
    before = (await client.get("/tc/221/rules", headers=READ)).json()

    async def fail(*a, **k):
        raise RuntimeError("qdrant went away")
    monkeypatch.setattr(service.store, "set_registry", fail)

    with pytest.raises(RuntimeError):
        await upload(client, 221, md_tc(gift_line="new rules"))
    assert await service.store.count(221, 2) == 0
    assert (await client.get("/tc/221/rules", headers=READ)).json() == before


async def test_leftovers_of_a_crashed_upload_are_cleaned(client, service):
    await upload(client, 221, md_tc())
    # simulate a crash that left version 2 chunks behind without switching the pointer
    await service.store.upsert_chunks(221, 2, [{
        "campaign_id": 221, "tc_version": 2, "chunk_index": 0, "chunk_id": "221-2-0000",
        "section": "x", "kind": "text", "text": "stale"}], [[0.1] * service.embedder.dim])
    r = (await upload(client, 221, md_tc(gift_line="v2"))).json()
    assert r["tc_version"] == 2
    rules = (await client.get("/tc/221/rules", headers=READ)).json()
    assert "stale" not in " ".join(c["text"] for c in rules["chunks"])
    assert await total_points(service, 221) == len(rules["chunks"])


async def test_concurrent_upload_same_campaign_gets_409(client, service):
    async with service.locks.hold(221):
        r = await upload(client, 221, md_tc())
    assert r.status_code == 409
    assert (await upload(client, 221, md_tc())).status_code == 200      # lock released


async def test_parallel_uploads_end_with_one_consistent_version(client, service):
    results = await asyncio.gather(*(upload(client, 221, md_tc(gift_line=f"variant {i}")) for i in range(4)))
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) >= 1 and set(codes) <= {200, 409}
    rules = (await client.get("/tc/221/rules", headers=READ)).json()
    assert await total_points(service, 221) == len(rules["chunks"])


async def test_search_finds_gift_table(client):
    await upload(client, 221, md_tc())
    r = await client.post("/tc/search", headers=READ,
                          json={"campaign_id": 221, "query": "gift for Galaxy S25 device", "top_k": 2})
    assert r.status_code == 200
    hits = r.json()["chunks"]
    assert hits[0]["section"] == "Gifts" and "score" in hits[0]


async def test_search_refuses_when_model_changed(client, service):
    await upload(client, 221, md_tc())
    reg = await service.store.get_registry(221)
    await service.store.set_registry(221, {**reg, "embed_model": "old-model"})
    r = await client.post("/tc/search", headers=READ, json={"campaign_id": 221, "query": "gift"})
    assert r.status_code == 409
    assert (await client.get("/tc/221/rules", headers=READ)).status_code == 200   # rules still work
    info = (await client.get("/tc/221", headers=READ)).json()
    assert info["search_ready"] is False


async def test_info_list_and_delete(client, service):
    await upload(client, 221, md_tc())
    info = (await client.get("/tc/221", headers=READ)).json()
    assert info["tc_version"] == 1 and info["stored_chunks"] == info["chunks"] == len(info["chunk_preview"])
    assert [c["campaign_id"] for c in (await client.get("/tc", headers=READ)).json()] == [221]

    d = await client.delete("/tc/221", headers=ADMIN)
    assert d.status_code == 200 and d.json()["deleted_version"] == 1
    assert (await client.get("/tc/221/rules", headers=READ)).json()["chunks"] == []
    assert await total_points(service, 221) == 0
    assert (await client.delete("/tc/221", headers=ADMIN)).status_code == 404
    assert (await client.get("/tc/221", headers=READ)).status_code == 404


async def test_dry_run_stores_nothing(client, service):
    r = await client.post("/tc/221/upload?dry_run=true", headers=ADMIN, files={"file": ("tc.md", md_tc())})
    body = r.json()
    assert body["status"] == "dry_run" and body["tc_version"] is None and body["preview"]
    assert await total_points(service, 221) == 0


@pytest.mark.parametrize("name,data,status", [
    ("tc.doc", b"whatever", 415),
    ("scan.png", b"\x89PNG....", 415),
    ("fake.pdf", b"not a pdf at all", 415),
    ("fake.docx", b"plain text", 415),
    ("empty.txt", b"", 422),
    ("blank.md", b"\n\n   \n", 422),
])
async def test_bad_files(client, name, data, status):
    r = await upload(client, 221, data, name=name)
    assert r.status_code == status, r.text


async def test_too_large(client):
    r = await upload(client, 221, b"a" * (1024 * 1024 + 10), name="big.txt")
    assert r.status_code == 413


async def test_auth(client):
    assert (await client.get("/tc/221/rules")).status_code == 401
    assert (await client.get("/tc/221/rules", headers={"Authorization": "Bearer nope"})).status_code == 403
    assert (await client.get("/tc/221/rules", headers=ADMIN)).status_code == 200        # admin can read
    r = await client.post("/tc/221/upload", headers=READ, files={"file": ("tc.md", md_tc())})
    assert r.status_code == 403                                                        # read can't write
    assert (await client.delete("/tc/221", headers=READ)).status_code == 403
    assert (await client.get("/health")).status_code == 200                            # open


async def test_invalid_campaign_id(client):
    assert (await client.get("/tc/0/rules", headers=READ)).status_code == 422
    assert (await client.get("/tc/abc/rules", headers=READ)).status_code == 422
