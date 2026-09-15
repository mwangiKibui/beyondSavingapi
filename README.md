# beyondSaving API

Backend API for beyondSaving.

Tasks are tracked in the [beyondSaving project board](https://github.com/users/mwangiKibui/projects/4), sourced from `docs/mvp1-tasks.md` in the local `beyondSaving` planning workspace (`ab-` issue codes match that list).

Companion frontend: [beyondSavingUI](https://github.com/mwangiKibui/beyondSavingUI).
Companion database (schema, migrations, seed data): [beyondSavingdb](https://github.com/mwangiKibui/beyondSavingdb).

## Running the full stack locally

These three repos are expected to be cloned as siblings (e.g. all under one
`beyondSaving/` folder) — this repo owns the docker-compose stack that the
other two connect to.

**1. Infra + API** (this repo) — Postgres, Redis, MinIO, the schema
migrations (run automatically, see below), and the FastAPI app:

```
cp .env.example .env
docker compose up -d --build
curl http://localhost:8000/health
# -> {"status":"ok","env":"development","redis":"ok","storage":"ok","db":"ok"}
```

- API: http://localhost:8000
- MinIO console: http://localhost:9001 (default `minioadmin` / `minioadmin`)
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

## Structure

```
app/
  main.py         # FastAPI app instance, routes, lifespan (ensures MinIO bucket exists)
  core/
    config.py     # Settings (pydantic-settings) — reads env vars / .env
    redis.py      # Redis client (cache + future job queue)
    storage.py    # S3-compatible (MinIO) client for uploaded files
```

Config is centralized in `app.core.config.Settings`, loaded once via
`get_settings()`. Values come from the process environment (set by
docker-compose's `environment:` block in dev) with `.env` as a fallback
for running outside Docker.
