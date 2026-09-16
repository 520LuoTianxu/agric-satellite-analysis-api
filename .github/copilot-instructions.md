# agric-satellite-analysis backend contribution notes

This repository contains the FastAPI API, Celery workers, migrations, Docker
Compose files and deployment scripts. The Next.js frontend lives in the
sibling repository `agric-satellite-analysis-web`.

## Architecture

- `services/api/`: FastAPI, SQLAlchemy, Alembic and API-side task dispatch.
- `services/ingest/`: satellite, weather and soil ingestion worker.
- `services/storage/`: lean OSS upload worker.
- `services/mq_consumer/` and `services/mq_result_writer/`: optional message
  queue services.
- `packages/agric_satellite_analysis_common/`: shared settings, storage,
  Celery and MQ helpers. Import the Python package as
  `agric_satellite_analysis_common`.
- `docker-compose.yml`: database, Redis, API, workers and the sibling web
  repository build.

The only supported application schema is `agric_satellite`. Keep API and
worker database access consistent with that schema. Download-host services
must use the internal HTTP boundary and must not connect to the API machine's
Postgres or Redis.

## API and worker conventions

- All API routes use the `/v1` prefix.
- Org-scoped routes must use the `get_current_user` and `get_org_context`
  dependency chain, plus the appropriate role guard.
- Use async SQLAlchemy sessions in FastAPI routes and sync sessions only in
  Celery tasks.
- Keep the pagination envelope `{items, total, limit, offset}` and the soft
  delete behavior unchanged.
- Geometry is stored as `MultiPolygon(4326)` and must pass through the existing
  GeoJSON normalization helper.
- Add Chinese comments when introducing non-obvious business logic, data
  conversions, or third-party integrations.

## Commands

```bash
python3 scripts/check-env-parity.py
cd services/api && ruff check . && ruff format --check .
cd services/api && python -m unittest discover -s tests -v
cd ..
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

The full Compose build requires the sibling frontend repository at
`../agric-satellite-analysis-web`. Frontend lint, type-check, i18n checks and
builds run in that repository's own GitHub Actions workflow.
