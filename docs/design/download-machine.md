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
| `INGEST_SCENE_STRAGGLER_TIMEOUT_SEC` | `600` | compose / `.env` |
| `WORK_CLAIM_*` timeouts / retries / watchdog | see `.env.example` | `mq_consumer` |
| `INTERNAL_API_HTTP_*` TLS retries | 30s / 5s connect / 3 retries | ingest+satellite |
| `INTERNAL_API_MAX_INFLIGHT` | `2` | gateway concurrency cap |
| `INTERNAL_API_MAX_CONNECTIONS` / `KEEPALIVE` | `4` / `4` | httpx pool |
| `INTERNAL_API_PROGRESS_MIN_INTERVAL_SEC` | `15` | progress PATCH throttle |

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
