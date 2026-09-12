# Crop season + harvest detect — Implementation Plan

> **For Claude:** execute task-by-task. Design: `docs/design/2026-09-12-crop-season-and-harvest-detect.md`

**Goal:** Spring/summer corn season windows for pulls; detect harvest day from official NDVI (observation only).

**Working branch:** `feat/crop-season-harvest-detect`

## File map

| File | Change |
|------|--------|
| `services/ingest/app/core/crops.py` (+ api mirror if any) | `corn_spring` / `corn_summer`; alias `corn`→summer |
| `apps/web` crop list / refresh RS UI | Crop + preset + custom start/end dates |
| `services/api` backfill body | Pass `crop_key` + date windows |
| `services/ingest` decloud/drought season checks | Consume normalized windows |
| `services/ingest/app/core/harvest_detect.py` (new) | NDVI drop detector |
| `services/api` router | On-demand harvest detect endpoint |
| `apps/web` timeseries | Marker for harvest_date |
| tests | Unit tests for seasons + detector |

## Task 1: Crop catalog

- Add `corn_spring` (default months 4–8, peak 6–7), `corn_summer` (6–9, peak 7–8)
- Alias `corn` → `corn_summer` in normalize
- Labels ZH/EN; list_crops exposes both
- Tests for normalize + get_crop_season
- Commit

## Task 2: Window normalize helper

- `normalize_growing_seasons(raw, *, year=None) -> list[{start_date,end_date,label}]`
- Accept months[] / start_month-end_month / start_date-end_date
- Shared module used by API + ingest
- Tests
- Commit

## Task 3: Refresh RS UI + API

- UI: select crop (spring/summer corn); date range inputs; multi-window
- API publish includes crop_key + normalized growing_seasons
- Commit

## Task 4: Pipeline consumes windows

- decloud / drought in-season use normalized windows (not hard-coded 6–9 for all corn)
- Commit

## Task 5: Harvest detect

- `harvest_detect(official_points, window, thresholds) -> result`
- API GET or POST on-demand for land+window
- Frontend marker + evidence text
- Tests with synthetic NDVI series
- Commit

## Task 6: PR merge

- Push, PR, merge; rebuild ingest if needed
