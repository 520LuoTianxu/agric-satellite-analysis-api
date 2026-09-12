# Optional UnCRtainTS parcel-window cloud removal

Additive post-process after agri optical ingest. Raw Sentinel-2 lonlat products
are kept. A second product is stored only when the feature is on and the
scene/parcel cloud fraction is above 30%.

Default is **off**. Download machines without GPU or model weights are unchanged.

## Enable

1. Clone [PatrickTUM/UnCRtainTS](https://github.com/PatrickTUM/UnCRtainTS) and
   set `UNCRTAINTS_HOME` to that clone (so `src.backbones.uncrtaints` imports).
2. Download the `diagonal_1` checkpoint (use_sar, input_t=3). Do **not** commit
   weights to git. Point `UNCRTAINTS_CHECKPOINT_DIR` at the directory that
   contains `model.pth.tar` plus sibling `conf.json` (or the `diagonal_1`
   subfolder). `.pth` / `.pt` / `.ckpt` files are still accepted.
3. On the ingest worker, optionally `pip install torch` (CUDA build if you have
   a GPU). The worker picks CUDA when available and CPU otherwise.
4. Set ingest env (see `.env.example` and compose passthrough):

```
DECLOUD_ENABLED=1
UNCRTAINTS_CHECKPOINT_DIR=/models/uncrtaints
UNCRTAINTS_HOME=/opt/UnCRtainTS
UNCRTAINTS_CHECKPOINT_NAME=diagonal_1
DECLOUD_BACKEND=uncrtaints
DECLOUD_MODE=batch
DECLOUD_CLOUD_MIN_PCT=30
DECLOUD_STAC_CLOUD_MAX_PCT=100
DECLOUD_INPUT_T=3
DECLOUD_USE_SAR=1
```

`DECLOUD_MODE=batch` is the default when the feature is on. `per_scene` is a
fallback that only fires after neighbor windows are already in the local cache.

Restart the ingest worker.

## Sequence (new vs old)

**Old (phase 1):** each raw S2 write immediately enqueued
`process_parcel_decloud`. That task searched STAC again for plus/minus 45-day
neighbors, often before enough dates existed, then published a weak or empty
cloud-removed product too early.

**New (batch, default):**

1. Search STAC up to `DECLOUD_STAC_CLOUD_MAX_PCT` (default 100; still weekly-best
   cloud) for the job date range.
2. Download **parcel windows only** (never a full Sentinel scene). When decloud
   is on, the optical job also pulls the extra L2A bands UnCRtainTS needs and
   stores each window in scratch keyed by land, date, and sensor.
3. Write the **raw** S2 lonlat product and publish its OSS + MQ as each scene
   finishes (progress). Raw rows are never deleted.
4. After every raw scene in the job is stored, enqueue
   `decloud_parcel_batch`. That task buffers remaining S2 neighbors
   (plus/minus 45 days) and matching S1 windows, then waits until at least
   `DECLOUD_INPUT_T` usable S2 windows exist.
5. Only then run UnCRtainTS on cloudy targets and publish decloud OSS + MQ.
   Cloudy-date official picks therefore wait until decloud finishes or is
   skipped (`neighbors_not_ready` does not write a product).

`DECLOUD_MODE=per_scene` may still enqueue `process_parcel_decloud` for one
date, but only when the cache already has enough neighbors. Dates that are
still short go to the same batch path.

The batch (or per-scene) reconstruct uses the cached stack: current cloudy S2
12 L2A bands (B10 filled with zeros), nearest other S2 dates to make
`input_t=3`, and nearest S1 VV/VH from Planetary Computer. Product JSON is
uploaded with `put_bytes`.

## Products

| | Raw | Decloud |
| --- | --- | --- |
| `sensor` | `S2` | `S2` |
| `scene_id` | `stac_bridge_{date}_S2` | `stac_bridge_{date}_S2_decloud` |
| OSS key | `{date}_S2.json` | `{date}_S2_decloud.json` |
| `pixel_data.source` | `stac_direct` | `uncrtaints_decloud` |
| `decloud_quality` | (none) | `good` / `fair` / `bad` |

MQ extras include `source`, `decloud_quality`, `decloud_score`. The result
writer upserts by `(land_id, date, sensor, scene_id)`, so raw rows are not
replaced.

## Quality gate

After reconstruct, a heuristic scores the parcel window:

- RGB still too bright
- tiny change vs the cloudy raw RGB
- spatial std collapse (over-smoothed)
- NDVI far below clear neighbors in a +/- 45 day window

Only **`good`** may enter official drought, land-assessment
RS inputs, overview drought/weak-growth, and share optical drought series.

`fair` and `bad` are **always stored** (OSS + MQ upsert → `agri.parcel_scene_products`) whenever a
reconstruction produced a usable array, even if lonlat sampling found few
or weak pixels. Column averages (`ndvi_avg`, …) and `pixel_data.decloud_metrics`
(rgb / reconstr NDVI / neighbor NDVI / gap) are kept for audit; only
`decloud_quality=good` feeds drought / land metrics. Existing cloud>30% drought filters also skip them
(`cloud_cover_over_30` stays true; `decloud_quality` is not `good`).
Tooltips mark them as de-cloud that may be unreliable.

NDVI and similar growth charts additionally compare raw vs **good** decloud
against nearby clear-raw phenology. A good reconstruct that does not match
the crop calendar / neighbor canopy loses to raw.

## Parcel cloud (not window fill)

`parcel_cloud_cover_pct` is the in-polygon SCL cloud/shadow/cirrus fraction
(classes 3, 8, 9, 10), or the share of lonlat pixels with `clear==0`. Nodata
(class 0) and values outside Sen2Cor 1-11 are ignored, not treated as clear.
Missing SCL or an empty polygon mask stores NULL, not 0. It is **not** zonal
`quality_score` (finite pixels / padded window). That old formula stuck small
fields near 82.5% on every date.

If a stored parcel value is ~0% while STAC `eo:cloud_cover` is 80% or higher,
display and drought skip treat the 0 as missing and use STAC. Good cloud-removed
rows no longer write 0% parcel cloud as a drought flag; tooltips show STAC
(or the raw parcel metric) instead of a fake 0.

New writes set `pixel_data.parcel_cloud_source` to `scl` or `lonlat_clear`.
`cloud_cover` remains STAC `eo:cloud_cover`. `cloud_cover_over_30` follows the
parcel metric when present (and STAC when that parcel 0 is untrusted).

Rows written before this change have no source marker. Values near 70-90% while
STAC is under 40% are treated as missing (tooltips / official pick use STAC).
Optional cleanup SQL: `scripts/legacy-parcel-cloud-cover.sql`.

## Official pick (raw vs good decloud)

Both products stay stored. Drought / land metrics:

1. Truly clear raw (real parcel cloud <= 30%, or STAC when parcel is missing or
   legacy fill): prefer raw.
2. Cloudy raw (real parcel > 30%) and a **good** decloud exists: prefer good
   decloud.
3. Both exist and raw is borderline (parcel 20-40%) *or* STAC is clear while
   parcel is cloudy: pick the product whose NDVI (then NDMI) is closer to the
   median of nearby clear raw dates (plus/minus 45 days, else same calendar
   month). Tie-break: raw, then `scene_id`.
4. Fair/bad decloud never enter official drought series.

NDVI / EVI / similar growth charts use a physiology-aware pick: same neighbor
baseline as step 3, applied whenever both raw and good decloud exist, plus a
growing-season sanity check (Jun-Sep NDVI far below a green neighbor canopy
is treated as a poor reconstruct). Raw wins ties and wins when it matches
phenology better than a "good" decloud.

## Smoke tests (no weights)

```
DECLOUD_ENABLED=1
DECLOUD_BACKEND=dummy
```

`dummy` darkens RGB and lifts NIR on CPU. It is for wiring checks, not science.
CI unit tests mock nothing of the net: they score the gate with scalars only
(`PYTHONPATH=services/ingest python -m unittest services/ingest/tests/test_decloud.py`).

## Retrigger

One date (uses cache first, then STAC if the window is missing):

```
celery -A app.worker call app.tasks.decloud_uncrtaints.process_parcel_decloud \
  --args '["<field-uuid>", "<land_id>", "2024-07-15"]'
```

A whole job range (buffer neighbors, then decloud every cloudy target):

```
celery -A app.worker call app.tasks.decloud_uncrtaints.decloud_parcel_batch \
  --args '["<field-uuid>", "<land_id>", "2024-06-01", "2024-08-31"]'
```
