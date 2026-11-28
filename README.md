# beyondSaving API

Backend API for beyondSaving.

Tasks are tracked in the [beyondSaving project board](https://github.com/users/mwangiKibui/projects/4), sourced from `docs/mvp1-tasks.md` in the local `beyondSaving` planning workspace (`ab-` issue codes match that list).

Companion frontend: [beyondSavingUI](https://github.com/mwangiKibui/beyondSavingUI).
Companion database (schema, migrations, seed data): [beyondSavingdb](https://github.com/mwangiKibui/beyondSavingdb).

## Running the full stack locally

These three repos are expected to be cloned as siblings (e.g. all under one
`beyondSaving/` folder) — this repo owns the docker-compose stack that the
other two connect to.

**1. Infra + API** (this repo) — Postgres, Redis, RustFS object storage, the schema
migrations (run automatically, see below), and the FastAPI app:

```
cp .env.example .env
docker compose up -d --build
curl http://localhost:8000/health
# -> {"status":"ok","env":"development","redis":"ok","storage":"ok","db":"ok"}
```

- API: http://localhost:8000
- Interactive API docs (Swagger UI): http://localhost:8000/docs — also available as ReDoc at `/redoc`, or the raw schema at `/openapi.json`. Built into FastAPI, no setup needed.
- Object storage console (RustFS): http://localhost:9001/rustfs/console/ (default `storageadmin` / `storageadmin`)
- Postgres: `localhost:5432` (default `beyondsaving` / `beyondsaving`)

Migrations run via a one-shot `migrate` container (official
`migrate/migrate` image, reading the sibling `beyondSavingdb/migrations`
folder) that `app` waits on before starting — every `docker compose up`
brings the schema fully up to date with no manual step. It's idempotent,
safe to run on every startup.

Stop with `docker compose down` (add `-v` to also drop volumes and start
from an empty database next time).

**2. Sample data** (optional) — from the sibling `beyondSavingdb` repo (see
its README for full details):

```
cd ../beyondSavingdb
docker exec -i beyondsavingapi-postgres-1 psql -U beyondsaving -d beyondsaving < seed.sql
```

**3. Frontend** (optional, for exercising the API through the UI) — from the
sibling `beyondSavingUI` repo:

```
cd ../beyondSavingUI
npm install
npm run dev
```

## Object storage

Uploaded statements go to an S3-compatible bucket. The app only talks plain
S3 (boto3, SigV4, path-style), so **switching stores is env-only - no code
change**:

| Environment | Store | `STORAGE_ENDPOINT` | `STORAGE_REGION` | Bucket |
|---|---|---|---|---|
| Local | RustFS `storage` service in docker-compose | `http://storage:9000` (set by compose) | `us-east-1` (ignored by RustFS) | `statements`, created on startup |
| Test (ab-98) | DigitalOcean Spaces | `https://<region>.digitaloceanspaces.com` | the Spaces region, e.g. `fra1` | a private test bucket |
| Production (ab-104) | DigitalOcean Spaces | `https://<region>.digitaloceanspaces.com` | same region as the servers | a private, **versioned** bucket |

App settings (env vars on `app` and `worker`): `STORAGE_ENDPOINT`,
`STORAGE_REGION`, `STORAGE_ACCESS_KEY`, `STORAGE_SECRET_KEY`,
`STORAGE_BUCKET` (default `statements`).

**Deploying to Spaces (test/prod):**
- Leave the `storage` service out of the deployed compose file; point the
  app and worker at Spaces with the env vars above.
- One bucket per environment, in the same region as the servers. Keep it
  private: no CDN, no public file listing, no public URLs - these are bank
  statements.
- Separate access keys per environment, scoped to that bucket where Spaces
  allows it, stored as environment secrets (never in `.env` in git).
- Create the bucket in the DigitalOcean control panel before the first
  deploy. The app's startup `ensure_bucket` will create it if missing, but
  then it gets default settings - set versioning (prod) explicitly.
- To confirm before ab-98/ab-104 (not yet verified against DigitalOcean's
  docs): region choice (no Africa region at the time of writing), bucket
  versioning, per-bucket access keys, encryption at rest.
- Health: `GET /health` reports `"storage": "ok"` when the bucket is
  reachable with the configured keys.

**Local notes:**
- RustFS is pinned (`rustfs/rustfs:1.0.4`); bump the tag deliberately,
  never `latest`.
- Data lives in the `storage_data` volume. Statements uploaded while the
  stack used MinIO (before ab-263) are in the old `minio_data` volume,
  which RustFS can't read; they're no longer needed once an import is
  parsed. Remove it with `docker volume rm beyondsavingapi_minio_data`
  when you're ready.

## AI layout proposer (ab-314)

When no stored layout reads an upload and our own guess doesn't pass the balance check, the worker asks a DigitalOcean-hosted model for the layout before the upload reaches Columns.

- **What's sent:** page 1, masked (`app/parsers/profiles/masking.py`). Table headings and common statement words stay as they are. Every other value is reduced to its shape (`9` digit, `X`/`x` letter). Each line starts with its vertical position on the page, so the model can see how a row's lines sit around its date (ab-333). No names, numbers or amounts leave the server.
- **What comes back:** column roles, the date format, balance labels and the row grouping (whether a row's text starts on its date line, sits above it or around it), never rows. Our reader extracts every row; the balance check and the description check (most rows must have a description) decide whether the proposal is used. A passing one shows on Columns as pending suggestions.
- **Row grouping without the AI (ab-333):** when a layout's reads fail either check, every row grouping is tried and the best passing one is used: our own guess, a saved layout and the wizard's check all do this. Each PDF is extracted once per process (a small cache), so the extra tries cost little.
- **How well the model does:** `python scripts/eval_ai_layouts.py` (inside the worker container) runs the model on page 1 of every statement in the dev database plus generated ones and prints a report; the latest is in `docs/research/ai-layout-eval.md`.
- **Model:** `openai-gpt-oss-120b`. DigitalOcean hosts it itself: it doesn't store inputs or outputs, and doesn't train on them. See `docs/dynamic-statement-import/model-test.md` for why it was chosen.
- **Setup:** create a model access key in the DigitalOcean control panel (Inference → Model Access Keys), limited to that model. Model access keys can only be made there, not through the API or Terraform. Put it in `.env` as `DO_INFERENCE_KEY=...` and recreate the app and worker (`docker compose up -d app worker`): the worker calls the model, and the API needs the key to queue lookups, report their status and offer Retry. With no key, the AI step is off and uploads go straight to Columns.
- **Limits and usage:** `ai_layout_proposals_per_day` (plan limit, default 5) caps model calls per user. Every call is recorded in `ai_layout_proposals`, with its outcome and token counts.
- **Footers (ab-337):** the masked page keeps footer words ("Printed", "Generated"…) and the bottom lines of a long page, and the model may name `page_furniture`. A name becomes a skip rule only when it really starts a line at the top or bottom of a page.

## AI category suggestions (ab-336)

Business payees that nothing else recognises get a category from their name, shown in the review as "AI suggested" (`app/services/ai_categories.py`).

- **What's sent:** business names (`ai_name`) and category names; and, since ab-339, every other open row's **description with its identifiers masked** (below). Never amounts, balances, numbers or user ids.
- **Step 1, shared:** names go in batches of 25 with the default category names; the model answers a default, a new name, or "unknown". Answers are cached for everyone in `merchant_ai_answers` ("unknown" is asked again after 90 days).
- **Step 2, per user:** the answer is mapped onto the user's own categories, first by name and synonyms (`local_match`), else by a small call with only category names, cached in `user_category_ai_map` per category list.
- **When:** the worker queues a lookup after each import (`ai_category_jobs`), and the review queues one for an older upload with unanswered names. The review only reads the caches and shows `ai_pending` while a lookup runs. Any failure means no AI suggestion; nothing waits on it.
- **Order:** the last source. For a business payee it also replaces a description keyword's guess it disagrees with, never the user's own choices, other users' picks, the shared list or own-account transfers.
- **Masked descriptions (ab-339):** rows that aren't a business name and weren't handled at import go as `mask_description(text)`: any word containing a digit (phone, account and card numbers, references, transaction codes) and any date become `***`; words are kept, **names included** (developer decision 2026-11-26). A row with no word of 3+ letters left isn't sent. The model picks one of the user's categories, one of their **transfer reasons** (shown as a Funds transfer suggestion; the user still picks the accounts), a new category name, or unknown. These answers are **private to the user** (`user_ai_text_answers`, migration 44), never shared or suggested to anyone else.
- **Model:** `DO_CATEGORY_MODEL`, default `glm-5.3-flash` (chosen in the ab-256 spike: `docs/research/do-inference-model-choice.md`). The same `DO_INFERENCE_KEY` must allow it as well as the layout model.

## Shared payee list cache

Each process builds the shared payee list (`merchant_directory`,
`merchant_directory_keys`, `blocked_numbers`) once and reuses it
(`app/services/directory_cache.py`, ab-300). Every use reads a one-row
version of those tables (counts plus newest timestamps) and rebuilds when
it changes, so admin edits and new keys show up on the next request in
every process. A change made by hand in SQL that leaves the timestamps
alone (e.g. an `UPDATE` on `merchant_directory_keys` or `blocked_numbers`)
shows up within 10 minutes; bump `merchant_directory.updated_at` to make
it show at once.

## Search

`GET /search?q=&limit=5` (ab-274) powers the navbar search: the signed-in
user's transactions (description, counterparty, or exact amount with or
without separators), payees, money plans and accounts, each group capped
at `limit` with its `total`. `type=…&page=&page_size=` returns one group,
paged, for the full results page. `loans` is always empty until there is a
loans table. Pages and actions are matched in the UI.

- Words match in order with anything between them (`java house` finds
  `JAVA  HOUSE`), as a plain `ILIKE`.
- Transactions are narrowed to the user's account ids first
  (`t.account_id = ANY(ARRAY(SELECT id FROM accounts WHERE user_id = $1))`)
  so Postgres uses `idx_txn_account_date`. Joining `accounts` instead
  made it scan every user's rows.
- Payees key the newest 500 matching statement rows in Python, like the
  payee groups (no shared payee list is read), so a very broad query can
  report fewer payees than exist.
- No text index or migration was added. Measured locally (one user with
  5,000 rows in a 255k-row table): each SQL query 3–7 ms, payee keying
  ≤7 ms. If users reach ~50k rows each, consider `pg_trgm` + a GIN index
  on `transactions.description`.

## Structure

```
app/
  main.py         # FastAPI app instance, routes, lifespan (ensures the storage bucket exists)
  core/
    config.py     # Settings (pydantic-settings) — reads env vars / .env
    redis.py      # Redis client (cache + future job queue)
    storage.py    # S3 client for uploaded statements (RustFS locally, Spaces in test/prod)
```

Config is centralized in `app.core.config.Settings`, loaded once via
`get_settings()`. Values come from the process environment (set by
docker-compose's `environment:` block in dev) with `.env` as a fallback
for running outside Docker.
