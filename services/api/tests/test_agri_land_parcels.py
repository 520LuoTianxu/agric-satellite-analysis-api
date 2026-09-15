"""Unit tests for agric_satellite.land_parcels upsert helper (no live DB)."""

from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.services.agri_land_parcels import (
    SOURCE_FILE,
    UPSERT_LAND_PARCEL_SQL,
    build_land_parcel_upsert_params,
    build_openfarm_tile_id,
    ensure_agri_land_parcel_for_field,
)


class TileIdConventionTests(unittest.TestCase):
    def test_with_group(self) -> None:
        self.assertEqual(
            build_openfarm_tile_id("25106", "7694"),
            "p7694_t00001_a25106",
        )

    def test_without_group(self) -> None:
        self.assertEqual(
            build_openfarm_tile_id("25107"),
            "p_manual_t00001_a25107",
        )

    def test_blank_group_uses_manual(self) -> None:
        self.assertEqual(
            build_openfarm_tile_id("9", "  "),
            "p_manual_t00001_a9",
        )


class UpsertParamsTests(unittest.TestCase):
    def test_polygon_bbox_area_and_sql_shape(self) -> None:
        boundary = {
            "type": "Polygon",
            "coordinates": [
                [
                    [116.0, 39.0],
                    [116.1, 39.0],
                    [116.1, 39.1],
                    [116.0, 39.1],
                    [116.0, 39.0],
                ]
            ],
        }
        params = build_land_parcel_upsert_params(
            land_id="25106",
            boundary_geojson=boundary,
            land_name="郎吕坡村委会3号",
            group_id="7694",
            area_ha=2.0,
            tags=["agri:25106", "cdfinance_group:7694", "province:河北"],
            field_id="319c1a96-5111-44ed-b62b-c77dd4a780b3",
            farm_id="farm-1",
        )
        self.assertEqual(params["land_id"], "25106")
        self.assertEqual(params["tile_id"], "p7694_t00001_a25106")
        self.assertEqual(params["group_id"], "7694")
        self.assertEqual(params["land_area_mu"], 30.0)
        self.assertEqual(params["original_area_mu"], 30.0)
        self.assertEqual(params["source_file"], SOURCE_FILE)
        self.assertEqual(params["source_feature_index"], 0)
        self.assertEqual(params["min_lon"], 116.0)
        self.assertEqual(params["max_lat"], 39.1)
        self.assertEqual(params["province_name"], "河北")
        props = json.loads(params["source_properties"])
        self.assertEqual(props["field_id"], "319c1a96-5111-44ed-b62b-c77dd4a780b3")
        self.assertIn("ON CONFLICT (land_id) DO UPDATE", UPSERT_LAND_PARCEL_SQL)
        self.assertIn("CAST(:boundary_geojson AS jsonb)", UPSERT_LAND_PARCEL_SQL)
        self.assertIn("agric_satellite.land_parcels.source_file = :source_file", UPSERT_LAND_PARCEL_SQL)

    def test_group_from_tags_when_not_passed(self) -> None:
        boundary = {
            "type": "Polygon",
            "coordinates": [
                [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]
            ],
        }
        params = build_land_parcel_upsert_params(
            land_id="1",
            boundary_geojson=boundary,
            tags=["agri:1", "cdfinance_group:42"],
        )
        self.assertEqual(params["group_id"], "42")
        self.assertEqual(params["tile_id"], "p42_t00001_a1")


class EnsureUpsertTests(unittest.IsolatedAsyncioTestCase):
    async def test_skips_without_land_id(self) -> None:
        db = AsyncMock()
        field = MagicMock()
        field.tags_json = ["crop:wheat"]
        field.geom = object()
        ok = await ensure_agri_land_parcel_for_field(db, field)
        self.assertFalse(ok)
        db.execute.assert_not_called()

    async def test_executes_upsert_sql(self) -> None:
        from geoalchemy2.shape import from_shape
        from shapely.geometry import Polygon

        poly = Polygon(
            [(116.0, 39.0), (116.1, 39.0), (116.1, 39.1), (116.0, 39.1)]
        )
        field = MagicMock()
        field.id = "319c1a96-5111-44ed-b62b-c77dd4a780b3"
        field.farm_id = "farm-1"
        field.name = "郎吕坡村委会3号"
        field.area_ha = 1.5
        field.tags_json = ["agri:25106", "cdfinance_group:7694"]
        field.geom = from_shape(poly, srid=4326)

        db = AsyncMock()
        db.execute = AsyncMock()
        ok = await ensure_agri_land_parcel_for_field(db, field, land_id="25106")
        self.assertTrue(ok)
        db.execute.assert_awaited_once()
        args, kwargs = db.execute.await_args
        sql = str(args[0])
        self.assertIn("INSERT INTO agric_satellite.land_parcels", sql)
        self.assertIn("ON CONFLICT (land_id)", sql)
        bind = args[1]
        self.assertEqual(bind["land_id"], "25106")
        self.assertEqual(bind["tile_id"], "p7694_t00001_a25106")
        self.assertEqual(bind["land_area_mu"], 22.5)


if __name__ == "__main__":
    unittest.main()
