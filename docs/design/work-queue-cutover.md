# Work-queue cutover runbook (download-host isolation)

> Status: **D4 scaffolding** — safe defaults unchanged. Do **not** flip production
> download to `WORK_QUEUE_MODE=claim` or `INGEST_PG_WRITES=0` without the verification
> steps below.
>
> Parent design: `docs/design/download-host-no-direct-pg-redis.md`  
> Date: 2026-09-14

---

## 0. Safe defaults (leave these unless cutover is intentional)

| Host | Variable | Safe default |
|------|----------|--------------|
| API + download | `WORK_QUEUE_MODE` | `legacy` |
| Download | `INGEST_PG_WRITES` | `1` |
| Download | `INGEST_HTTP_WRITES` | `0` (unset) |
| Download | claim agent | only when `WORK_QUEUE_MODE=claim` |

**Double-dispatch rule:** `dual` may enqueue `work_items` **and** publish CloudAMQP.
Download must keep **MQ consumer only** under `dual` / `legacy`. Claim polling runs
**only** when download `WORK_QUEUE_MODE=claim`. Never set API=`dual` and download=`claim`
while MQ consumers are still processing the same task types.

---

## 1. Cutover order (high level)

```
1) API dual          → work_items filled; MQ still primary executor
2) Download HTTP canary → INGEST_HTTP_WRITES=1 (reads already via API_BASE_URL)
3) Download claim    → WORK_QUEUE_MODE=claim on download ONLY after dual backlog OK
                       (API may stay dual briefly, then API→claim)
4) HTTP writes force → INGEST_PG_WRITES=0 after job/UI checks
5) MQ teardown       → optional; stop mq_consumer AMQP + CloudAMQP when idle
```

Do **not** skip to step 3/4 on production without the checklists in §3–§4.

---

## 2. Phase A — API `WORK_QUEUE_MODE=dual` (enqueue only)

**Goal:** Insert `work_items` for claimable types while MQ remains the executor.

### Claimable types (D4)

| type | Typical producer | Claim agent dispatch |
|------|------------------|----------------------|
| `assessment_report` | assessment router | Celery assessment PDF |
| `season_growth_report` | season_growth router | Celery season-growth PDF |
| `field_bootstrap` | field create / one-click pull | weather + soil + agri optical/S1 backfill (+ optional followup report) |
| `satellite_analysis` | field backfill / mq tasks | agri optical + S1 chunk wave (`backfill_indices_for_field` + bridge) |
| `agri_bridge` | mq tasks | bridge-only |
| `weather_backfill` | weather refresh | weather Celery |
| `soil_fetch` | soil refresh | soil Celery |

Optical / S1 “chunks” are **not** separate `work_items` rows; they are Celery fan-out
under `satellite_analysis` / `field_bootstrap` (same as MQ handler).

### Steps

1. Deploy API build that includes D4 enqueue + claim agent coverage.
2. Set **API** `WORK_QUEUE_MODE=dual` (download stays `legacy`).
3. Smoke: create field / weather refresh / soil refresh / backfill / assessment.
4. Verify rows appear: `SELECT type, status, count(*) FROM work_items GROUP BY 1,2;`
5. Confirm download still executes via MQ only (no claim process).
6. Leave pending `work_items` accumulating or periodically purge test rows; they are
   not claimed until download is `claim`.

### Rollback

Set API `WORK_QUEUE_MODE=legacy`. MQ path unchanged.

---

## 3. Phase B — Download HTTP write canary

**Prereq:** `API_BASE_URL` + `INTERNAL_API_TOKEN` on download (D2 reads).

1. Set download `INGEST_HTTP_WRITES=1` (keep `INGEST_PG_WRITES=1`).
2. Run assessment / season_growth / weather path; confirm job progress updates and
   no error storms in ingest logs (`jobs/patch`, `results/apply`).
3. UI: PDF job reaches succeeded; weather/soil panels refresh.

### Rollback

Unset `INGEST_HTTP_WRITES` (or `0`).

---

## 4. Phase C — Download `WORK_QUEUE_MODE=claim` (executor cutover)

**Prereq:** Phase A stable; claim types covered; internal token shared; local Redis
for Celery only.

### Pre-flight checklist

- [ ] API has been on `dual` (or already `claim`) and `work_items` insert for the
      types you will claim.
- [ ] Download compose has `API_BASE_URL`, `INTERNAL_API_TOKEN`, `REDIS_URL=localhost`.
- [ ] **No** second consumer still claiming the same types (only one claim fleet).
- [ ] MQ consumer on this host will switch to claim-agent-only (entrypoint does this
      when mode=`claim`).
- [ ] If API is still `dual`, **stop or drain** CloudAMQP consumers for those types
      **before** enabling claim on download — otherwise dual MQ + claim = double work.
      Preferred: set API `WORK_QUEUE_MODE=claim` in the same change window as download
      claim (skip MQ publish entirely), **or** pause MQ consumers first.

### Flip sequence (recommended)

1. Pause MQ consumers (download `mq_consumer` scale 0 **or** API `WORK_QUEUE_MODE=claim`
   so new publishes stop).
2. Set download `WORK_QUEUE_MODE=claim`.
3. Start `mq_consumer` (runs claim agent only).
4. Set API `WORK_QUEUE_MODE=claim` if not already (stops MQ publish + keeps enqueue).
5. Smoke: weather, soil, satellite backfill, field create bootstrap, assessment,
   season_growth (with and without `pull_data`).
6. Watch: `work_items` pending → leased → done; lease reaper; ingest Celery; UI.

### Verification queries / signals

- Pending not growing unbounded while workers idle.
- No duplicate Celery task storms for same `idempotency_key` / job_id.
- Assessment one-click (`pull_data`): `field_bootstrap` work item with
  `followup_assessment` (not a parallel naked `assessment_report` race).

### Rollback

1. Download `WORK_QUEUE_MODE=legacy` (+ restart MQ consume).
2. API `WORK_QUEUE_MODE=dual` or `legacy`.
3. Optionally fail/requeue stuck `leased` rows after lease expiry (reaper).

---

## 5. Phase D — `INGEST_PG_WRITES=0`

**Prereq:** Phase B canary green; claim or HTTP write path proven for jobs you care about.

1. Set download `INGEST_PG_WRITES=0`.
2. Confirm ingest no longer opens SyncSession writes for gated paths; failures
   surface via HTTP.
3. UI regression: PDFs, weather, soil.

### Rollback

`INGEST_PG_WRITES=1`.

**Do not** set this on production download until Phase C (or at least Phase B) is verified.

---

## 6. Phase E — MQ teardown (optional)

1. Confirm no publishers (`WORK_QUEUE_MODE=claim` on API) and no consumers.
2. Drain CloudAMQP queues; remove `CLOUDAMQP_URL` from download when unused.
3. Keep `mq_consumer` service name as claim agent host, or rename later.

---

## 7. D4.1 — assessment / season-growth / critical ingest reads (no download PG)

| Endpoint / path | Purpose |
|-----------------|---------|
| `GET /v1/internal/fields/{id}/assessment-bundle` | Full `load_field_bundle` JSON for scoring/PDF |
| `GET /v1/internal/fields/{id}/season-growth-inputs` | Field + S2/S1/indices rows for `build_season_facts` |
| `GET /v1/internal/fields/{id}/data-readiness` | Weather/soil/RS counts for bootstrap wait |
| ingest `data_loader` / season `facts` | Prefer HTTP when `API_BASE_URL`+token; PG only if `INGEST_PG_READS` allows |
| weather / soil tasks | Field centroid via `fields/{id}/geom`; HTTP-only upsert via `results/apply` when `INGEST_PG_WRITES=0` |

**Still deferred (heavy):** high-volume `raster_layers` / `field_stats` scene upserts; `pg_advisory_*` backfill locks; per-chunk optical work_items.

After D4.1 verify assessment PDF for a known field with download `DATABASE_URL` pointing at a closed `:5432` (or unset) while `API_BASE_URL` works.

---

## 8. Hot-patch notes

- Prefer hot-patch **API** first (enqueue + internal routes + `result_apply`).
- Download image: claim agent + `openfarm_common` HTTP helpers.
- Never ship production download env with `claim` / `INGEST_PG_WRITES=0` as compose defaults.

---

## 9. Quick decision tree

| Intent | API mode | Download mode | PG writes |
|--------|----------|---------------|-----------|
| Prod today (safe) | `legacy` | `legacy` | `1` |
| Fill work_items, MQ still runs | `dual` | `legacy` | `1` |
| HTTP write canary | any | `legacy` + `INGEST_HTTP_WRITES=1` | `1` |
| No-MQ executor | `claim` | `claim` | `1` then `0` after verify |
