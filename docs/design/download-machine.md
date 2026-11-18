# Download machine bootstrap

Production download hosts run Celery workers + an HTTP claim agent. They talk to
the API through the joint-venture gateway, use **local Redis only** as the
Celery broker, and do **not** open Postgres or publish task results over CloudAMQP.

## Compose files

```bash
cp .env.example .env
# Fill REQUIRED secrets in the "Download machine" section of .env
# Set a unique WORKER_NAME / WORKER_ID for this host

docker compose \
  -f docker-compose.yml \
  -f docker-compose.dev.yml \
  -f docker-compose.download-machine.yml \
  --profile consumer \
  --profile local-redis \
  up -d --build redis ingest satellite storage beat mq_consumer decloud
```

Profiles:

| Profile | Purpose |
|---|---|
| `local-redis` | Local Celery broker (`redis://127.0.0.1:6379/0`) |
| `consumer` | Enables `mq_consumer` (claim agent when `WORK_QUEUE_MODE=claim`) |
| `local-db` / `api` / `web` / `producer` | Disabled on download hosts |

Do **not** start `beat` on more than one download machine at a time (duplicate
daily / weekly schedules).

## Required `.env` (non-secret defaults already in compose / `.env.example`)

| Key | Live / required value |
|---|---|
| `API_BASE_URL` | `https://joint-venture.cdfinance.com.cn/satellite-api` |
| `API_PATH_PREFIX` | empty (gateway has no `/v1`) |
| `INTERNAL_API_TOKEN` | same secret as API host (**REQUIRED**) |
| `WORKER_NAME` / `WORKER_ID` | unique per machine (**REQUIRED**) |
| `WORK_QUEUE_MODE` | `claim` |
| `INGEST_PG_WRITES` / `INGEST_PG_READS` | `0` |
| `INGEST_HTTP_WRITES` | `1` |
| `DATABASE_URL` / `DATABASE_URL_SYNC` | blank |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` |
| `CLOUDAMQP_RESULT_PUBLISH` | `0` |
| `OSS_ENDPOINT` | `https://{bucket}.oss-accelerate.aliyuncs.com` |
| `OSS_IS_CNAME` | `true` (virtual-hosted accelerate) |
| `OSS_ACCESS_KEY_*` / `OSS_BUCKET` | prod credentials (**REQUIRED**) |

Receipt / `public_url` hosts stay on the ordinary regional virtual-host domain
(`https://{bucket}.{OSS_REGION}.aliyuncs.com`) even when uploads use accelerate.

## Proven operational knobs (compose defaults)

| Knob | Value | Where |
|---|---|---|
| satellite Celery concurrency | `6` | `docker-compose.download-machine.yml` command |
| ingest / decloud / storage concurrency | `2` | compose command |
| `INGEST_SCENE_MAX_WORKERS` | `8` (decloud override `2`) | compose env |
| `INGEST_SCENE_MAX_INFLIGHT` | `1` | compose / `.env` (scenes in windowed IO per worker) |
| `INGEST_SCENE_STRAGGLER_TIMEOUT_SEC` | `180` | compose / `.env` |
| `BAND_READ_TIMEOUT_SEC` | `20` | compose / `.env` (HTTP read/total + parent wall) |
| `BAND_READ_HEDGE_AFTER_SEC` | `5` | compose / `.env` (second connection if first byte is late) |
| `GDAL_SLOT_WAIT_SEC` | `20` | compose / `.env` (fail a band if no slot is free) |
| `BAND_SCENE_MAX_TIMEOUTS` / `BAND_SCENE_BUDGET_SEC` | `2` / `60` | compose / `.env` (skip remaining doomed bands) |
| `BAND_WINDOW_CACHE` | `1` | compose / `.env` (shared scene-window cache) |
| `GDAL_HTTP_VERSION` / `MULTIRANGE` | `1.1` / `SERIAL` | compose / `.env` (leftover GDAL HTTP) |
| `GDAL_HTTP_TIMEOUT` | `20` | compose / `.env` |
| `WORK_CLAIM_*` timeouts / retries / watchdog | see `.env.example` | `mq_consumer` |
| `INTERNAL_API_HTTP_*` TLS retries | 30s / 5s connect / 3 retries | ingest+satellite |
| `INTERNAL_API_MAX_INFLIGHT` | `2` | gateway concurrency cap |
| `INTERNAL_API_MAX_CONNECTIONS` / `KEEPALIVE` | `4` / `4` | httpx pool |
| `INTERNAL_API_PROGRESS_MIN_INTERVAL_SEC` | `45` | progress PATCH throttle |
| `INTERNAL_API_CACHE_BUFFER` / `FLUSH_MAX` / `FLUSH_SEC` | `1` / `8` / `2` | coalesce results/cache POSTs |

## Windowed COG downloads

Windowed S2/S1 band reads no longer send HTTP Range traffic through GDAL
`/vsicurl/` / libcurl, whose timeouts often failed to cancel on the long path
to `us-west-2`. `httpx` issues the Range GETs (connect 10s, read/total 20s).
If the first request has not succeeded after 5s, a second connection is
started and the first success wins, unless a recent window already has a high
timeout rate (hedge circuit). rasterio/GDAL only decode the fetched header and
tiles. A global cap of 8 slots stays in place; waiting for a slot times out at
20s, and a timeout immediately frees the slot so a later 1-2s job can run.
Two real hung-read timeouts (not slot-wait admission failures) or 60s of in-slot IO skip the rest of that scene. `INGEST_SCENE_MAX_INFLIGHT=1` admits one scene at a time per worker so its bands can finish. The
existing scene-window cache is on by default in compose so retries do not
re-hit AWS.

## What must stay out of git

- `.env`, `*.env.live`, `*.env.full`, `download-machine.env*`, `dotenv.live`
- Any real `INTERNAL_API_TOKEN`, OSS AK/SK, CloudAMQP URL, JWT secrets

Only `.env.example` is tracked. Ask an operator for a private full env snapshot
when bringing up a new machine.

## Quick health check

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml \
  -f docker-compose.download-machine.yml --profile consumer --profile local-redis ps

# claim loop should log work/claim 200 about every WORK_CLAIM_INTERVAL_SEC
docker compose logs -f --tail=50 mq_consumer satellite
```
