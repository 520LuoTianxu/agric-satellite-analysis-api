# Agri drought and flood classification

Field timeseries, heatmaps, overview, and land-assessment optical inputs share
these rules. Implementation: `services/api/app/core/agri_classify.py` (mirrored
in ingest) and `apps/web/src/lib/agri-classify.ts`.

Decloud stays optional (`DECLOUD_ENABLED` default off). Raw S2 rows are never
deleted. Fair/bad decloud products are stored for audit only.

## Official optical product

For a calendar date:

1. Prefer **clear raw** S2 (`cloud ≤ 30%`, not a `_decloud` scene).
2. Else use **good** decloud (`decloud_quality=good`).
3. Fair/bad decloud never enter official NDVI, drought, overview, or land RS.
4. Cloudy raw without a good decloud may still plot on the NDVI chart (tooltip
   says cloudy / no de-cloud) but drought skips it (`unreliable`).

## Drought (scene / date class)

Growing season only: **June-September** (`PHENOLOGY_MONTHS`, overridable).

Baselines (same calendar month, all years) use **official** scenes only.

A date is drought when **both**:

- NDDI is dry: absolute NDDI ≥ 0.3 **or** same-month NDDI percentile ≥ p80
  (needs ≥ 3 official month samples)
- Confirmation: NDMI < 0.10 (or ≤ month median − 0.05) **or** NDVI ≤ month
  median − 0.08

Severity then follows NDDI 0.4 / 0.5 (and large NDVI drops 0.12 / 0.20).

Classes: `normal` / `mild` / `moderate` / `severe` / `unreliable` /
`out_of_season`. Pixel heatmaps still paint NDDI 0.3 / 0.4 / 0.5 bands on
official in-season dates.

## Flood (Sentinel-1)

VV/VH already live on agri scene products (`vv_avg` / `vh_avg`). Classification
is client-side from that series; the API only exposes orbit when parseable.

**Flood** if all of:

- parcel median VV ≤ −17 dB
- VV − per-orbit baseline ≤ −3 dB (baseline = median VV of valid scenes in
  that relative-orbit group; ≥ 3 samples, else all-scene median)
- helper: VH ≤ −22 dB **or** VV−VH ≤ per-orbit p40

**Watch** is near-threshold. VV−VH alone never flags flood.

Spring (Mar-May) detections may be puddling or irrigation, not disaster flood.
The field UI states that next to the flood series.

Pixel flood paint uses low VV plus the VH helper (no orbit drop at pixel
scale). Confirmed flood vs watch is the date-level series.
