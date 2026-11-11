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
