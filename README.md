# beyondSaving API

Backend API for beyondSaving.

Tasks are tracked in the [beyondSaving project board](https://github.com/users/mwangiKibui/projects/4), sourced from `docs/mvp1-tasks.md` in the local `beyondSaving` planning workspace (`ab-` issue codes match that list).

Companion frontend: [beyondSavingUI](https://github.com/mwangiKibui/beyondSavingUI).

## Local development

Stack: Python (FastAPI), Postgres, Redis, MinIO.

```
cp .env.example .env
docker compose up -d --build
curl http://localhost:8000/health
```

- API: http://localhost:8000
- MinIO console: http://localhost:9001 (default `minioadmin` / `minioadmin`)
- Postgres: `localhost:5432` (default `beyondsaving` / `beyondsaving`)

Stop with `docker compose down` (add `-v` to also drop volumes).
