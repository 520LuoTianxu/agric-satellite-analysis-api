# 0030 Use a local projected grid for parcel analysis

- Status: Accepted
- Date: 2026-09-28

## Context

The optical, Sentinel-1, decloud, and shared satellite-batch paths need a common
analysis grid. A WGS84 grid with a degree step derived from the parcel center is
only approximately metric: east-west spacing changes with latitude, and some
Sentinel-1 paths previously constructed a separate fixed `0.0001°` grid. This
made grid spacing and cell alignment depend on the processing path.

Pixel products are persisted as `lonlat_v1`, STAC searches use WGS84 geometry,
and source COGs must be windowed using WGS84 bounds. Those external contracts
remain independent from the internal analysis CRS.

## Decision

1. Build analysis grids in the local UTM CRS selected from the WGS84 bounds
   center; use UPS for polar latitudes where UTM is not defined.
2. Use 10 metres as the nominal target cell size. Keep the existing 5,000-cell
   edge limit and 4,000,000-cell total limit; when a larger area reaches either
   limit, lower the actual grid resolution and report the resulting spacing.
3. Keep source-query bounds and `lonlat_v1` pixel coordinates in WGS84. Reproject
   source bands, parcel masks, and COG outputs into the selected analysis CRS;
   transform sampled pixel centers back to WGS84 before publication.
4. A shared batch scene uses one UTM CRS centered on its processing window. Each
   parcel is reprojected from that shared window into its own parcel-centered
   UTM grid before masking and publication.
5. Include CRS in analysis-grid metadata and band-window cache identity. Move
   decloud array caches to a versioned directory and key each array by CRS,
   transform, dimensions, and parcel mask so stale pixel arrays cannot be used
   with a changed grid or boundary. Bump optical/S1 product versions.
6. Reject non-finite or out-of-range bounds and projection failures rather than
   silently falling back to a geographic grid.

## Consequences

- Pixel distances and areas are measured in metres over the intended local
  agricultural extent, and all primary S1/S2 analysis paths use the same grid
  construction and limits.
- One multi-parcel batch may contain arrays in several parcel CRSs after the
  shared read. The explicit reprojection into each parcel grid costs CPU but
  keeps per-parcel pixel metadata internally consistent.
- COGs produced by the analysis paths now carry projected CRS metadata. Any
  consumer of those COGs must honor the embedded CRS rather than assume WGS84.
- The 10 m target is a common sampling grid, not a claim about native sensor
  resolution. Sentinel-2 20 m bands remain resampled; large extents may also be
  coarsened by the cell cap. Both facts remain visible in product metadata.
- Existing published products remain valid historical records with their
  existing CRS metadata. New outputs carry new algorithm versions; reruns
  replace them through the existing idempotent product keys.
- Old decloud scratch arrays are left in their previous cache directory and are
  ignored. Operators may remove that derived cache after the new pipeline has
  been deployed and confirmed.

## Alternatives considered

- Keep a WGS84 grid with center-derived geodesic step: preserves the simplest
  compatibility but still varies in scale across wide or high-latitude parcels.
- Use a custom local azimuthal-equidistant CRS: suitable for arbitrary local
  areas, but introduces custom CRS serialization and operational complexity
  where the project’s normal parcel and processing windows are regional.
- Use one fixed regional CRS for every parcel: simplifies batch reuse but can
  add distortion for parcels far from its central meridian and complicates
  future use outside the current region.

## Validation

- Static checks must cover every grid consumer, including S1 HTTP/local paths,
  batch reprojection, decloud cache reads, COG metadata, and lon/lat sampling.
- A projected-grid smoke check must confirm the CRS, mask alignment, and actual
  cell spacing for a typical parcel and a cell-capped large extent.
- Rasterio/GDAL projection and source-scene quality still require representative
  S1/S2 production fixtures; no real satellite sample is bundled with the repo.
