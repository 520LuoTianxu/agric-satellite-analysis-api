# MySQL land sync

## Scope

The API machine reads the source MySQL join of `agriculture_land` and
`agriculture_land_group`. PostgreSQL `agric_satellite.farms` and
`agric_satellite.land_parcels` remain the canonical application data. Download
workers never receive the MySQL credentials and do not read the source DB.

## Schedule and flow

`land_sync` runs once daily at 23:00 `Asia/Shanghai`. It holds the PostgreSQL
advisory lock `agric-satellite:mysql-land-sync`, streams the MySQL snapshot in
batches, validates the pipe-delimited WGS84 polygon, and commits each target
batch before dispatching remote-sensing work.

New or boundary-changed parcels receive a deterministic backfill job covering
the previous 24 calendar months. The task is dispatched as
`satellite_analysis`, so both S2 optical indices and S1 VV/VH products use the
existing download and result-ingest pipeline. Metadata-only changes do not
re-download two years of imagery.

Each download-host parcel task uses a 5 km × 5 km square centered on the
parcel centroid for STAC search and raster reads. Products and statistics are
still masked by the original parcel polygon. For grouped daily jobs, only
parcels whose complete boundary is covered by the anchor parcel's square are
included; a parcel crossing the square boundary is excluded from that group.
An oversized anchor parcel is processed independently using its complete
bounding rectangle.

## Source-specific decisions

- `land_area` is stored as `land_area_mu` and is treated as mu (亩).
- `wgs_land_path` is parsed as `lon,lat|lon,lat|...`, normalized to a
  `MultiPolygon`, and used to derive `area_ha` and the WGS84 bbox.
- `planting_type` and `business_category` are preserved in `source_properties`;
  they are not mapped to `crop_type`.
- `status` is preserved as `source_properties.source_status`; it is not mapped
  to `land_status`, whose meaning is coordinate conversion readiness.
- Existing `tile_id` values are preserved. New rows use
  `mysql_group_{group_id}` as a stable processing fallback.
- Source-owned rows missing from a completed non-empty source snapshot are
  soft-deleted. API-created rows are not touched.

## Operations

Keep `MYSQL_SOURCE_URL` only in the API-machine private `.env`. Grant the
MySQL account `SELECT` on the two source tables, restrict MySQL network access
to the API machine, and inspect `AuditEvent(event_type='mysql_land_sync')` plus
the generated `backfill` jobs when a run is partial.
