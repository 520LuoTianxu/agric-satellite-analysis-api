# AGENTS.md

Agent rules for this repo. Broader product and stack guidance is in `CLAUDE.md`. Keep `.github/copilot-instructions.md` in sync when conventions change.

## Download host: no direct Postgres

Hard rule. Download-side processes must not open a session to the API Postgres cluster.

Applies to: `ingest`, `storage`, `beat`, `decloud`, `mq_consumer`, and any new worker that runs under `docker-compose.download-machine.yml`.

Do not:

- Set or use `DATABASE_URL` / `DATABASE_URL_SYNC` on the download host to reach API `:5432`.
- Discover work by querying `fields`, `land_parcels`, `jobs`, `raster_layers`, or `work_items` from the download host.
- Write weather, soil, scenes, or job status from the download host via SQLAlchemy / raw SQL.
- Point download-host `REDIS_URL` at the API Redis (`:6379`). Local Redis is the Celery broker only.

Do:

- Read land/job/scene metadata through Internal HTTP (`API_BASE_URL` + `INTERNAL_API_TOKEN`).
- Take work with `POST /v1/internal/work/claim`; report via progress / complete / fail or `POST /v1/internal/results/apply`.
- Keep Postgres access on the API machine. If a download worker needs a list of lands to fetch, add an Internal HTTP list endpoint on the API and call it from ingest. Do not `SELECT` that list on the download host.
- Beat schedules use: `POST /v1/internal/schedule/weekly-index`, `GET /v1/internal/schedule/weather-lands`, `POST /v1/internal/schedule/overview-refresh`.
- Treat `ALLOW_LEGACY_DB=1` / `INGEST_PG_WRITES=1` as a temporary cutover escape hatch, not as a design to extend.

Celery Beat (`services/ingest/app/beat.py`) only publishes task names onto the download-host Redis. It must not query Postgres. Scheduled tasks that decide *which* lands to download must ask the API over HTTP.

Design: `docs/design/download-host-no-direct-pg-redis.md`. Cutover: `docs/design/work-queue-cutover.md`.
