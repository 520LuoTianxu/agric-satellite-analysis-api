-- Remove persisted 5km/VPA10 groupings and shared pixel-asset metadata.
-- The active 10km planner is stateless; per-run boundaries live only in Job.params_json.
BEGIN;

DROP VIEW IF EXISTS agric_satellite.v_virtual_project_areas_detail;
DROP TABLE IF EXISTS agric_satellite.virtual_project_area_assets;
DROP TABLE IF EXISTS agric_satellite.virtual_project_area_lands;
DROP TABLE IF EXISTS agric_satellite.virtual_project_areas;

COMMIT;
