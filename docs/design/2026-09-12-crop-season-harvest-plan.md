# Crop season + harvest detect — Implementation Plan

> **For Claude:** execute task-by-task. Design: `docs/design/2026-09-12-crop-season-and-harvest-detect.md`

**Goal:** Spring/summer corn season windows for pulls; detect harvest day from official NDVI (observation only).

**Working branch:** `feat/crop-season-harvest-detect`

## File map

| File | Change |
|------|--------|
| `services/ingest/app/core/crops.py` (+ api mirror if any) | Keep `corn` default 6–9; aliases 春/夏玉米→corn; no spring/summer keys |
| `apps/web` refresh RS UI | List of windows: date range + crops[] (≤2) + presets |
| `services/api` backfill body | Pass `crop_key` + date windows |
| `services/ingest` decloud/drought season checks | Consume normalized windows |
| `services/ingest/app/core/harvest_detect.py` (new) | NDVI drop detector |
| `services/api` router | On-demand harvest detect endpoint |
| `apps/web` timeseries | Marker for harvest_date |
| tests | Unit tests for seasons + detector |

## Task 1: Crop catalog (clarified)

- Do **not** add `corn_spring` / `corn_summer` keys
- Keep `corn` default season 6–9; aliases 春玉米/夏玉米 → corn
- Soften corn season label to generic 玉米季（默认…）
- Tests for normalize + get_crop_season
- Commit

## Task 2: Window normalize helper

- `normalize_growing_seasons(raw, *, year=None) -> list[{start_date,end_date,crops,label?}]`
- Accept months[] / start_month-end_month / start_date-end_date
- Normalize legacy `crop` → `crops: [crop]`; enforce ≤2 crops/window
- Shared module used by API + ingest
- Tests
- Commit

## Task 3: Refresh RS UI + API

- UI: list of windows; each: date range + multi-select crops (≤2); presets for 春/夏玉米 dates
- API: GrowingSeasonWindow.crops[]; validate ≤2/window; forward normalized growing_seasons
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
