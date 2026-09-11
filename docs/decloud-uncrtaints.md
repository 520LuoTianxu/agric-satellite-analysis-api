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
   contains the `.pth` / `.pt` file (or the `diagonal_1` subfolder).
3. On the ingest worker, optionally `pip install torch` (CUDA build if you have
   a GPU). The worker picks CUDA when available and CPU otherwise.
4. Set ingest env (see `.env.example` and compose passthrough):

```
DECLOUD_ENABLED=1
UNCRTAINTS_CHECKPOINT_DIR=/models/uncrtaints
UNCRTAINTS_HOME=/opt/UnCRtainTS
UNCRTAINTS_CHECKPOINT_NAME=diagonal_1
DECLOUD_BACKEND=uncrtaints
DECLOUD_CLOUD_MIN_PCT=30
DECLOUD_STAC_CLOUD_MAX_PCT=80
DECLOUD_INPUT_T=3
DECLOUD_USE_SAR=1
```

Restart the ingest worker. Optical jobs then:

- search STAC up to `DECLOUD_STAC_CLOUD_MAX_PCT` (still weekly-best cloud)
- write the raw S2 lonlat product as today
- if parcel/scene cloud > 30%, enqueue `app.tasks.decloud_uncrtaints.process_parcel_decloud`

That task reads **only the field polygon window** (never a full Sentinel scene):
current cloudy S2 12 L2A bands (B10 filled with zeros), nearest other S2 dates
to make `input_t=3`, and nearest S1 VV/VH from Planetary Computer.

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

Only **`good`** may enter official drought, growth timeseries, land-assessment
RS inputs, overview drought/weak-growth, and share optical series.

`fair` and `bad` are stored for audit. Existing cloud>30% drought filters also
skip them (`cloud_cover_over_30` stays true).

## Smoke tests (no weights)

```
DECLOUD_ENABLED=1
DECLOUD_BACKEND=dummy
```

`dummy` darkens RGB and lifts NIR on CPU. It is for wiring checks, not science.
CI unit tests mock nothing of the net: they score the gate with scalars only
(`PYTHONPATH=services/ingest python -m unittest services/ingest/tests/test_decloud.py`).

## Retrigger

```
celery -A app.worker call app.tasks.decloud_uncrtaints.process_parcel_decloud \
  --args '["<field-uuid>", "<land_id>", "2024-07-15"]'
```
