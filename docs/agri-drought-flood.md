# Agri drought and flood classification

Field timeseries, heatmaps, overview, and land-assessment optical inputs share
these rules. Implementation: `services/api/app/core/agri_classify.py` (mirrored
in ingest) and `agric-satellite-analysis-web/src/lib/agri-classify.ts`.

Fair/bad decloud products are stored for audit only and never enter drought
or other official land metrics. NDVI and similar growth charts compare raw
versus good decloud against nearby clear-raw phenology and prefer raw when
it fits the crop calendar better.

## Official optical product

For a calendar date:

1. Prefer **clear raw** S2 when the **in-polygon** cloud is <= 30% (SCL
   cloud/shadow fraction, or lonlat `clear==0` share). STAC scene cloud is
   stored separately in `cloud_cover`. Old rows whose parcel cloud sits near
   82% while STAC is much lower are treated as the padded-window fill bug, not
   real cloud; those dates fall back to STAC. Parcel ~0% while STAC is 80% or
   higher is also treated as missing (not a true clear field). Missing parcel
   cloud is shown as none, not 0%. If both parcel and STAC cloud are missing,
   only an explicit `cloud_cover_over_30=false` can qualify the raw scene;
   absent cloud-quality metadata remains unknown and is excluded from official
   drought baselines.
2. If raw is cloudy (real parcel > 30%) and a **good** decloud exists, use
   good decloud.
3. If both exist and raw is borderline (parcel 20-40%) *or* STAC is clear
   while parcel is cloudy, pick the product whose NDVI (then NDMI) is closer
   to the median of nearby clear raw dates (plus/minus 45 days, else same
   month). Tie-break: raw, then scene id.
4. Fair/bad decloud never enter official drought, overview, or land RS.
   Quality and product-source markers ignore surrounding whitespace and case
   differences so older write paths use the same product classification.
5. **NDVI / growth series** (`pick_optical_for_ndvi`): when both raw and a
   **good** decloud exist, pick the product whose NDVI (then NDMI) is closer
   to the median of nearby clear raw dates, and that is not absurd versus
   the growing-season canopy (June-September: NDVI far below a green
   neighbor baseline loses). Tie-break: raw. Fair/bad decloud never become
   the plotted official point; they may appear as marked "may be unreliable"
   overlays.
6. Cloudy raw without a good decloud may still plot on the NDVI chart
   (tooltip says cloudy / no de-cloud) but drought skips it (`unreliable`).

Cloud-removal search default is 100% STAC cloud (`DECLOUD_STAC_CLOUD_MAX_PCT`).
Decloud runs when parcel cloud > 30% **or** STAC cloud > 30%, up to that max.

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
- VV − comparable-scene baseline ≤ −3 dB (median VV within the same relative
  orbit when it has ≥ 3 valid scenes; otherwise use all relative orbits only
  within the same platform, calibration epoch, processing version, method,
  and scale when that group has ≥ 3 scenes)
- helper: VH ≤ −22 dB **or** VV−VH ≤ p40 from ≥ 3 comparable VV/VH scenes

If neither group has enough samples, the baseline is unavailable. A
water-like single-scene signal may remain `watch`; otherwise the date is
unclassified rather than `dry` or confirmed flood. This sample floor is an
operational guard, not a substitute for field validation.

The field timeseries evaluates each date from observations no later than that
date, so a later acquisition cannot change an earlier date's flood label.
Scenes from the target calendar date remain in the date-level baseline, matching
the existing same-day aggregation rule. Loading older history can add earlier
reference scenes and resolve dates that were previously unclassified.

**Watch** is near-threshold. VV−VH alone never flags flood.

Spring (Mar-May) detections may be puddling or irrigation, not disaster flood.
The field UI states that next to the flood series.

Pixel flood paint uses low VV plus the VH helper (no orbit drop at pixel
scale). Confirmed flood vs watch is the date-level series.
