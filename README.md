# TC RAG API

Uploads a campaign's Terms & Conditions, splits it into rule-sized chunks, embeds them into
Qdrant keyed by `campaign_id`, and serves them to the
[validation agent](https://github.com/samsungmmka-cmd/ocr-agentic) through the contract it already uses.

```
Admin / CMS ──POST /tc/{campaign_id}/upload──► tc-rag-api ──► Qdrant (tc_chunks + tc_registry)
                                                    │                 ▲
                                    read → chunk → embed → write N+1 │ → switch to N+1 → delete N
                                                                      │
Validation agent ──GET /tc/{campaign_id}/rules / POST /tc/search──────┘  (active version only)
```

## Replacing a campaign's TC

Uploading a TC for a campaign that already has one **replaces it completely**: the old chunks
are deleted, but only after the new ones are safely stored.

| Step | What happens |
|---|---|
| 1 | The file is read and chunked. A bad file fails here; the current TC is untouched |
| 2 | Same file (SHA-256) as the active version and same embedding model → `unchanged`, nothing re-embedded. `force=true` re-embeds anyway |
| 3 | Leftovers of a crashed upload for this campaign are removed |
| 4 | Every chunk is embedded and written as version N+1 |
| 5 | The number of stored N+1 chunks is checked |
| 6 | The registry pointer switches to N+1. From this moment claims are validated against the new TC |
| 7 | Version N is deleted |

If anything fails in steps 4–6, the partial N+1 is deleted and N stays active, so a campaign is
never left without rules, and old and new chunks are never mixed. One upload / delete per
campaign runs at a time (Redis lock); a second one gets `409`.

## How documents are chunked

The agent sends **all** of a campaign's chunks to the LLM, so chunks are built as clean, complete rules:

- **Sections**: headings come from Markdown `#`, Word Heading styles, PDF font size / bold, or
  numbered and CAPS titles (`3. Gifts`, `SCHEDULE A`). Every chunk carries its `section`
  (two levels deep, e.g. `Gifts > Gift table`). A chunk never mixes two sections.
- **Tables** become Markdown tables in their own chunk and are never cut mid-row. A table above
  `TABLE_MAX_TOKENS` is split by rows into equal parts, and **every part repeats the header row**
  plus a note like `(Table part 1 of 2: rows 1-39 of 72)`. A sentence ending in `:` right before
  a table ("Each claim receives the gift listed below:") stays with the table.
- **PDF specifics**: a table that continues over several pages is merged back into one table
  (repeated header rows dropped); repeated page headers / footers and page numbers are removed;
  numbered clauses (`9.1`, `9.2`…) and bullets stay separate paragraphs.
- **Long sections** are split at paragraph, then sentence boundaries, never mid-sentence, at
  `CHUNK_MAX_TOKENS` (~700).
- **Overlap** is 0 by default: the agent reads every chunk in order, so overlap would show the
  same rule twice. It is configurable for search-heavy use.
- **What is embedded**: `section + text`, so a search for "gift for Galaxy S25" finds the Gifts table.

Supported: `.pdf` (text PDFs), `.docx`, `.txt`, `.md`. Scanned / image-only PDFs, `.doc` and
images are rejected with a clear error. Use `?dry_run=true` to see exactly how a file will be
chunked before uploading it for real.

**Large documents**: the vector size (1536 for `text-embedding-3-small`) is per chunk, not per
document, so document size is not limited by it. A large TC simply gives more chunks; every chunk
stays well under the model's input limit.

## Run with Docker

```bash
cp .env.example .env        # set EMBED_API_KEY and the two API tokens
docker compose up --build   # api :8100, qdrant :6333, redis
```

No API key yet? Set `EMBED_MODE=hash` to run everything locally (dev only: it matches words,
not meaning). Any OpenAI-compatible endpoint works: set `EMBED_BASE_URL`, `EMBED_MODEL`, `EMBED_DIM`.

Already running Qdrant and Redis from the shared `infra/` compose? Run only the API:
```bash
pip install -r requirements.txt
cp .env.example .env        # QDRANT_URL=http://localhost:6333, REDIS_URL=redis://:redis-dev-pass@localhost:6379/0
uvicorn app.main:app --port 8100
```

## Connect the validation agent

In the agent's `.env`:
```env
RAG_API_URL=http://localhost:8100          # from Docker: http://host.docker.internal:8100
RAG_API_KEY=<API_READ_TOKEN of this service>
```
Nothing else changes in the agent. With the sample TCs below uploaded, all 13 agent sample
claims give the same decisions, scores and gifts as with the agent's mock RAG.

## Try it

```bash
TOKEN=change-me-admin-token
curl -X POST localhost:8100/tc/221/upload -H "Authorization: Bearer $TOKEN" -F file=@samples/tc_221_galaxy_promo.pdf
curl -X POST localhost:8100/tc/222/upload -H "Authorization: Bearer $TOKEN" -F file=@samples/tc_222_mtn_voucher.docx
curl -X POST localhost:8100/tc/223/upload -H "Authorization: Bearer $TOKEN" -F file=@samples/tc_223_choose_gift.md

# replace campaign 223's TC (adds a 4th gift option)
curl -X POST localhost:8100/tc/223/upload -H "Authorization: Bearer $TOKEN" -F file=@samples/tc_223_choose_gift_v2.md
# -> {"status": "replaced", "tc_version": 2, "replaced_version": 1, ...}

# preview the chunks without storing anything
curl -X POST "localhost:8100/tc/221/upload?dry_run=true" -H "Authorization: Bearer $TOKEN" -F file=@samples/tc_221_galaxy_promo.pdf

curl localhost:8100/tc/221/rules -H "Authorization: Bearer change-me-read-token"
curl localhost:8100/tc/221      -H "Authorization: Bearer change-me-read-token"   # metadata + chunk preview
```

Or use Swagger at http://localhost:8100/docs (click *Authorize*, paste the token).

## API

| Endpoint | Token | Description |
|---|---|---|
| `POST /tc/{campaign_id}/upload` | admin | multipart `file`, optional form `force=true`, query `dry_run=true` |
| `GET /tc/{campaign_id}/rules` | read | All chunks of the active version, in document order. Unknown campaign → `200` with `tc_version: null, chunks: []` (the agent flags "no TC rules") |
| `POST /tc/search` | read | `{campaign_id, query, top_k}` → chunks with `score`, active version only |
| `GET /tc/{campaign_id}` | read | Active version metadata + preview of every chunk |
| `GET /tc` | read | All campaigns with a TC |
| `DELETE /tc/{campaign_id}` | admin | Removes the campaign's TC |
| `GET /health` | – | Qdrant and Redis checks |

Tokens are sent as `Authorization: Bearer <token>`. The admin token can also read.

**Upload response**
```json
{"campaign_id": 223, "status": "replaced", "tc_version": 2, "replaced_version": 1,
 "file_name": "tc_223_choose_gift_v2.md", "file_hash": "…", "doc_title": "Galaxy Choose-Your-Gift Promotion 2026 - Terms and Conditions",
 "chunks": 9, "table_chunks": 1, "sections": ["Promotion period", "…", "Gifts", "Gift values"],
 "embed_model": "text-embedding-3-small", "duration_ms": 412, "preview": null}
```
`status`: `created`, `replaced`, `unchanged`, `dry_run`.

**Rules response** (what the agent reads; it uses `section` and `text`)
```json
{"campaign_id": 221, "tc_version": 1, "chunks": [
  {"id": "221-1-0007", "section": "Gifts", "kind": "table", "chunk_index": 7, "page": 1, "part": null,
   "text": "Each valid claim receives the gift listed for the purchased device:\n| Device | Gift |\n|---|---|\n| Galaxy A55 | R500 Takealot voucher |\n…"}]}
```

**Errors**: `401/403` token, `409` campaign busy or (search only) embedded with a different model,
`413` too large, `415` file type, `422` unreadable / empty / scanned file, `502` embedding provider,
`503` Qdrant.

## Qdrant layout

| Collection | Points | Payload |
|---|---|---|
| `tc_chunks` | one per chunk, id = uuid5(campaign, version, index) | `campaign_id`, `tc_version`, `chunk_id`, `chunk_index`, `section`, `kind`, `text`, `page`, `part`, `tokens`, `file_name`, `doc_title`, `embed_model`, `created_at` |
| `tc_registry` | one per campaign, id = `campaign_id` (no vector) | active `tc_version`, file name / type / size / hash, chunk counts, sections, `embed_model`, `embed_dim`, `uploaded_at`, `previous_version` |

`campaign_id` and `tc_version` are indexed. The collections and indexes are created at startup.
If `tc_chunks` already exists with another vector size, the service refuses to start.

**Changing the embedding model**: use a new `QDRANT_COLLECTION` (vectors of different models must
not share a collection) and re-upload the TCs. Until then `/rules` keeps working and `/search`
returns `409` for campaigns embedded with the old model.

## Project layout

```
app/
  main.py          FastAPI routes, auth, error mapping
  service.py       upload (versioned replace + rollback), rules, search, delete
  chunker.py       blocks → chunks (sections, tables, splitting)
  readers/         pdf_reader.py, docx_reader.py, text.py (txt + md), base.py (headings, tables)
  embedder.py      OpenAI-compatible embeddings with retries; hash embedder for dev
  store.py         Qdrant: chunks + registry
  locks.py         per-campaign lock (Redis, or in-process)
  schemas.py       request / response models
  config.py        settings from .env
samples/           TCs for campaigns 221 (PDF), 222 (DOCX), 223 (MD + v2)
scripts/           make_samples.py (regenerates samples/)
tests/             pytest, no network (in-memory Qdrant, fake Redis)
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Production notes

- Several API instances can run behind a load balancer; the Redis lock keeps uploads per campaign serial.
- Logs are JSON lines with `campaign_id`, `tc_version`, file name and timings.
- Before going live, upload each real TC with `?dry_run=true` and read the chunks once: headings
  and tables in real documents vary, and this is the cheapest place to catch a bad split.
- The original files are not stored. Keep them in your CMS; re-upload with `force=true` to re-embed.
