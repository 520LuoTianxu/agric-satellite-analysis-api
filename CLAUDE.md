# agric-satellite-analysis backend

This repository owns the FastAPI API, Celery workers, SQL schema scripts,
Docker Compose stack and deployment scripts for agric-satellite-analysis. The
Next.js application is maintained in the sibling repository
`agric-satellite-analysis-web`.

## Workspace layout

```text
agric-satellite-analysis-workspace/
├── agric-satellite-analysis-api/   # this backend repository
└── agric-satellite-analysis-web/   # standalone Next.js repository
```

The backend Compose file keeps the `web` service for the full stack, but its
Docker build context is `../agric-satellite-analysis-web`. Do not restore the
legacy monorepo frontend directory or copy frontend source into this
repository.

## Commands

### Backend API

```bash
cd services/api
pip install -e "[dev]"
# Schema changes are SQL files under scripts/, never run on API startup.
psql "$DATABASE_URL_SYNC" -v ON_ERROR_STOP=1 -f ../../scripts/convert_postgis_geometry_to_jsonb.sql
uvicorn app.main:app --reload --port 8000
ruff check .
ruff format --check .
python -m unittest discover -s tests -v
```

### Full stack

```bash
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

Run frontend-only commands from the sibling repository:

```bash
cd ../agric-satellite-analysis-web
npm ci
npm run dev
npm run lint
npm run type-check
npm run build
python scripts/check-i18n-keys.py
```

## Architecture invariants

- The only application schema is `agric_satellite`.
- All API routes use the `/v1` prefix and preserve the pagination envelope.
- Org-scoped routes use the existing JWT, organization and role dependency
  chain.
- FastAPI routes use async SQLAlchemy sessions. Celery tasks use sync sessions.
- Geometry is normalized to `MultiPolygon(4326)` through the existing helper.
- Download-host services use the internal HTTP boundary and do not connect to
  the API machine's Postgres or Redis.
- Object storage is Aliyun OSS. Do not add MinIO dependencies or services.

## Shared package

The shared package lives at
`packages/agric_satellite_analysis_common/` and is imported as
`agric_satellite_analysis_common`. The old `openfarm_common` namespace must
not be reintroduced.

## Change guidelines

Add Chinese comments for non-obvious business logic, data conversion,
algorithm choices and third-party integrations. Keep secrets out of Git. For
database changes, add and review an Alembic migration. For frontend changes,
send the corresponding change to the standalone web repository.
