"""Claim/download-host tests for the decloud HTTP data plane."""

from __future__ import annotations

import os
import unittest
from datetime import date
from unittest.mock import patch

import numpy as np
from shapely.geometry import mapping, box


class DecloudHttpDataPlaneTests(unittest.TestCase):
    def test_empty_database_urls_select_http_mode(self) -> None:
        from app.tasks import decloud_uncrtaints as decloud

        with (
            patch.dict(
                os.environ,
                {
                    "API_BASE_URL": "http://api.test",
                    "INTERNAL_API_TOKEN": "token",
                    "DATABASE_URL": "",
                    "DATABASE_URL_SYNC": "",
                    "INGEST_PG_WRITES": "1",
                    "INGEST_PG_READS": "1",
                },
                clear=False,
            ),
            patch("app.core.http_mode.ingest_http_only", return_value=False),
        ):
            self.assertTrue(decloud._decloud_http_only())

    def test_configured_database_keeps_legacy_mode(self) -> None:
        from app.tasks import decloud_uncrtaints as decloud

        with (
            patch.dict(
                os.environ,
                {
                    "API_BASE_URL": "http://api.test",
                    "INTERNAL_API_TOKEN": "token",
                    "DATABASE_URL": "postgresql://api-db/openfarm",
                    "DATABASE_URL_SYNC": "",
                    "INGEST_PG_WRITES": "1",
                    "INGEST_PG_READS": "1",
                },
                clear=False,
            ),
            patch("app.core.http_mode.ingest_http_only", return_value=False),
        ):
            self.assertFalse(decloud._decloud_http_only())

    def test_land_context_uses_internal_http_without_session(self) -> None:
        from app.tasks import decloud_uncrtaints as decloud

        boundary = mapping(box(110.0, 35.0, 110.01, 35.01))
        remote = {
            "land_id": "L1",
            "tile_id": "T1",
            "land_name": "地块一",
            "boundary_geojson": boundary,
        }
        with (
            patch.object(decloud, "_decloud_http_only", return_value=True),
            patch(
                "app.core.http_mode.resolve_land_http", return_value=remote
            ) as resolve_land,
            patch.object(
                decloud,
                "compute_target_grid",
                return_value=("transform", (1, 1), np.ones((1, 1), dtype=bool), (0, 0, 1, 1)),
            ),
        ):
            context = decloud._land_context(None, "L1")

        resolve_land.assert_called_once_with("L1")
        self.assertEqual(context["land_meta"]["tile_id"], "T1")
        self.assertEqual(context["land_meta"]["land_name"], "地块一")
        self.assertEqual(context["land_geom_geojson"]["type"], "Polygon")

    def test_neighbor_ndvi_uses_http_scene_rows(self) -> None:
        from app.tasks import decloud_uncrtaints as decloud

        target = date(2026, 7, 15)
        with patch(
            "agric_satellite_analysis_common.internal_api.season_growth_inputs",
            return_value={
                "s2_rows": [
                    {"date": "2026-07-15", "ndvi_avg": 0.1, "official": True},
                    {"date": "2026-07-01", "ndvi_avg": 0.6, "official": True},
                    {"date": "2026-06-20", "ndvi_avg": 0.8, "official": True},
                    {"date": "2026-06-01", "ndvi_avg": 0.2, "official": False},
                ]
            },
        ) as scene_inputs:
            value = decloud._neighbor_ndvi(None, "L1", target)

        self.assertAlmostEqual(value, 0.7)
        self.assertEqual(scene_inputs.call_args.args[0], "L1")
        self.assertEqual(scene_inputs.call_args.kwargs["date_from"], "2026-05-31")
        self.assertEqual(scene_inputs.call_args.kwargs["date_to"], "2026-08-29")

    def test_batch_does_not_open_session_in_http_mode(self) -> None:
        from app.tasks import decloud_uncrtaints as decloud

        context = {
            "land_meta": {"land_id": "L1", "tile_id": "T1", "land_name": "L1"},
            "land_geom_geojson": mapping(box(110.0, 35.0, 110.01, 35.01)),
            "target_transform": "transform",
            "target_shape": (1, 1),
            "land_mask": np.ones((1, 1), dtype=bool),
            "bounds": (110.0, 35.0, 110.01, 35.01),
        }
        with (
            patch.object(decloud, "decloud_enabled", return_value=True),
            patch.object(decloud, "_decloud_http_only", return_value=True),
            patch.object(decloud, "get_db_session") as get_db,
            patch.object(decloud, "_land_context", return_value=context),
            patch.object(decloud, "_buffer_s2_windows", return_value=[{"date": date(2026, 7, 15)}]),
            patch.object(decloud, "decloud_use_sar", return_value=False),
            patch.object(
                decloud,
                "_decloud_one_from_buffer",
                return_value={"status": "ok", "published": {"json_url": "oss://x"}},
            ),
        ):
            result = decloud.decloud_parcel_batch.run(
                "L1",
                "2026-07-01",
                "2026-07-31",
                targets=[{"date": "2026-07-15", "stac_cloud": 80.0}],
                season_months=[7],
            )

        get_db.assert_not_called()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["published"], 1)

    def test_published_product_uses_http_result_cache(self) -> None:
        from app.tasks import decloud_uncrtaints as decloud

        index_arrays = {
            key: np.ones((1, 1), dtype=np.float32)
            for key in ("NDVI", "EVI", "NDMI", "NDRE", "CIre", "MNDWI")
        }
        pixels = [
            {
                "lon": 110.0,
                "lat": 35.0,
                "NDVI": 0.5,
                "EVI": 0.2,
                "NDMI": 0.1,
                "NDRE": 0.1,
                "CIre": 0.1,
                "MNDWI": 0.1,
            }
        ]
        with (
            patch.object(decloud, "_decloud_http_only", return_value=True),
            patch(
                "app.tasks.bridge_stac_cogs_to_agri_lonlat._sample_lonlat",
                return_value=pixels,
            ),
            patch(
                "app.tasks.bridge_stac_cogs_to_agri_lonlat._stats",
                return_value=(0.5, 0.1, 0.9),
            ),
            patch(
                "app.tasks.agri_lonlat.publish_optical_lonlat_to_oss_mq",
                return_value="https://oss.test/decloud.json",
            ) as publish,
        ):
            result = decloud._publish_decloud_product(
                meta={"land_id": "L1", "tile_id": "T1", "land_name": "L1"},
                date_str="2026-07-15",
                land_id_str="L1",
                index_arrays=index_arrays,
                transform="transform",
                geom4326={},
                quality=decloud.DecloudQualityResult("good", 0.9, []),
                raw_scene_id="raw-s2",
                stac_cloud=80.0,
                parcel_cloud=80.0,
                mq_task_id="task-1",
                quality_inputs=decloud.DecloudQualityInputs(
                    rgb_mean=0.2,
                    rgb_mean_raw=0.4,
                    rgb_std=0.08,
                    rgb_std_raw=0.09,
                    ndvi_mean=0.5,
                ),
            )

        self.assertEqual(result["json_url"], "https://oss.test/decloud.json")
        self.assertEqual(publish.call_args.kwargs["result_delivery"], "http")


if __name__ == "__main__":
    unittest.main()
