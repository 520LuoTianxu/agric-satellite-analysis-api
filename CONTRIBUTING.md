# Contributing to agric-satellite-analysis

This repository contains the backend and deployment code. The frontend is
maintained independently at
[agric-satellite-analysis-web](https://github.com/520LuoTianxu/agric-satellite-analysis-web).

## Workspace setup

Keep both repositories under one outer directory:

```text
agric-satellite-analysis-workspace/
├── agric-satellite-analysis-api/
└── agric-satellite-analysis-web/
```

```bash
git clone https://github.com/520LuoTianxu/agric-satellite-analysis-api.git
git clone https://github.com/520LuoTianxu/agric-satellite-analysis-web.git
cd agric-satellite-analysis-api
```

The sibling layout is required when Docker Compose builds the `web` service.

## Backend development

Prerequisites are Python 3.11+, Docker Compose v2 and a running Postgres/PostGIS
and Redis instance, or the local Compose stack.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e packages/agric_satellite_analysis_common
pip install -e "services/api[dev]"

cd services/api
alembic upgrade head
ruff check .
ruff format --check .
python -m unittest discover -s tests -v
```

The shared package is imported as
`agric_satellite_analysis_common`. The old `openfarm_common` package name is
not supported.

## Full stack

```bash
cp .env.example .env
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

The main services are available at:

| Service | URL |
| --- | --- |
| Web UI | http://localhost:3000 |
| API docs | http://localhost:8000/docs |
| PostgreSQL | localhost:5432 |
| Redis | localhost:6379 |

The frontend repository contains its own `npm ci`, lint, type-check, build and
i18n commands. Run those commands from
`../agric-satellite-analysis-web`.

## Architecture rules

- The only application schema is `agric_satellite`.
- API routes use the `/v1` prefix and the existing pagination envelope.
- Org-scoped endpoints use the JWT, organization and role dependency chain.
- FastAPI routes use async SQLAlchemy sessions. Celery tasks use sync sessions.
- Geometry is normalized to `MultiPolygon(4326)` through the existing helper.
- Download-host services use the internal HTTP boundary and do not connect to
  the API machine's Postgres or Redis.
- Object storage uses Aliyun OSS. Do not add MinIO services or dependencies.
- Add Chinese comments for non-obvious business logic, data conversion,
  algorithm choices and third-party integrations.

## Changes and pull requests

1. Create a feature branch from `main`.
2. Keep backend and frontend changes in their respective repositories. If an
   API contract changes, describe the matching frontend change in the PR.
3. Run the backend checks and, when both repositories are available, the full
   Compose build.
4. Add and review an Alembic migration for database schema changes.
5. Never commit secrets or modify the production environment file.
