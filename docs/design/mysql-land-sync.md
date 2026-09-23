# MySQL land sync

## Scope

The API machine reads the source MySQL join of `agriculture_land` and
`agriculture_land_group`. PostgreSQL `agric_satellite.farms` and
`agric_satellite.land_parcels` remain the canonical application data. Download
workers never receive the MySQL credentials and do not read the source DB.

## Schedule and flow

`land_sync` runs once daily at 22:00 `Asia/Shanghai`. It holds the PostgreSQL
advisory lock `agric-satellite:mysql-land-sync`, streams the MySQL snapshot in
batches, validates the pipe-delimited WGS84 polygon, and commits each target
batch before dispatching remote-sensing work.

New or boundary-changed parcels are matched to an existing fully containing
10×10 km virtual project area or planned into a new one. The API dispatches
shared `satellite_batch` jobs covering the previous 24 calendar months for S1
and S2. Full-window pixel assets are reused from OSS when available; on a miss,
the project area is downloaded once, then cropped results are persisted for
the selected parcels. Metadata-only changes do not trigger a historical pull.

The project-area boundary is persistent and does not move to the incoming
parcel's centroid. If an incoming geometry no longer fits its assigned area,
the old membership is marked stale and the planner selects a replacement area.

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
the generated `smart_land_sync_satellite` parent jobs and `satellite_batch`
children when a run is partial.
