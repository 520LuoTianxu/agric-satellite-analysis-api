"""验证共享下载、HTTP边界及逐地块像元隔离。"""

import unittest
from datetime import date
from unittest.mock import patch

import numpy as np
from rasterio.transform import xy
from shapely.geometry import Point, box, mapping
from shapely.ops import unary_union

from app.tasks import satellite_batch as batch
from app.tasks.pipeline import compute_target_grid


def remote_land(land_id):
    offset = 0 if land_id == "A" else 0.01
    return {
        "land_id": land_id,
        "tile_id": "tile",
        "boundary_geojson": mapping(box(110 + offset, 35, 110.002 + offset, 35.002)),
    }


def make_lands(sensor="S2"):
    with (
        patch.object(
            batch,
            "resolve_land",
            side_effect=lambda **kwargs: remote_land(kwargs["land_id"]),
        ),
        patch.object(batch, "agri_scene_dates", return_value=[]),
    ):
        return batch._load_lands(["A", "B"], sensor, False)


class SharedWindowTests(unittest.TestCase):
    def test_optical_download_once_and_publish_only_requested_lands(self):
        job = {
            "status": "pending",
            "params_json": {
                "land_ids": ["A", "B"],
                "sensor": "S2",
                "date_from": "2026-08-01",
                "date_to": "2026-08-01",
            },
        }
        scene = {
            "id": "scene",
            "date": date(2026, 8, 1),
            "cloud_cover": 0,
            "band_hrefs": {"B04": "red", "B08": "nir"},
            "geometry": mapping(box(109, 34, 111, 36)),
        }

        def read_bands(hrefs, bounds, target_shape, target_transform, **kwargs):
            return {key: np.ones(target_shape, dtype=np.float32) for key in hrefs}

        with (
            patch.object(batch, "get_job", return_value=job),
            patch.object(batch, "patch_job") as progress,
            patch.object(
                batch,
                "resolve_land",
                side_effect=lambda **kwargs: remote_land(kwargs["land_id"]),
            ),
            patch.object(batch, "agri_scene_dates", return_value=[]),
            patch.object(batch, "search_scenes_for_defs", return_value=[scene, scene]),
            patch.object(
                batch, "read_bands_windowed_parallel", side_effect=read_bands
            ) as read,
            patch.object(batch, "_publish_land", return_value=True) as publish,
            patch.object(batch, "decloud_enabled", return_value=False),
            patch("app.tasks.pipeline.get_db_session") as db,
        ):
            result = batch.process_satellite_batch.run("job")
        read.assert_called_once()
        self.assertEqual(
            [call.args[2]["meta"]["land_id"] for call in publish.call_args_list],
            ["A", "B"],
        )
        self.assertEqual(result["products_published"], 2)
        self.assertEqual(
            result["published_products"],
            [
                {"land_id": "A", "date": "2026-08-01"},
                {"land_id": "B", "date": "2026-08-01"},
            ],
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(progress.call_args.args[1]["status"], "completed")
        db.assert_not_called()

    def test_existing_dates_and_sensor_footprint_skip_before_download(self):
        lands = make_lands()
        scene = {
            "date": date(2026, 8, 1),
            "cloud_cover": 0,
            "geometry": mapping(box(109.99, 34.99, 110.004, 35.004)),
        }
        selected = batch._scene_lands(scene, lands, "S2")
        self.assertEqual([land["meta"]["land_id"] for land in selected], ["A"])
        lands[0]["existing"].add(scene["date"])
        self.assertEqual(batch._scene_lands(scene, lands, "S2"), [])

    def test_http_scene_dates_are_a_list_and_force_skips_lookup(self):
        with (
            patch.object(batch, "resolve_land", return_value=remote_land("A")),
            patch.object(
                batch, "agri_scene_dates", return_value=["2026-08-01"]
            ) as dates,
        ):
            lands = batch._load_lands(["A"], "S2", False)
            self.assertEqual(lands[0]["existing"], {date(2026, 8, 1)})
            forced = batch._load_lands(["A"], "S2", True)
        self.assertEqual(forced[0]["existing"], set())
        dates.assert_called_once()

    def test_failed_scene_is_reported_as_failed(self):
        job = {
            "status": "pending",
            "params_json": {
                "land_ids": ["A"],
                "sensor": "S1",
                "date_from": "2026-08-01",
                "date_to": "2026-08-01",
            },
        }
        scene = {"id": "s1", "date": date(2026, 8, 1)}
        with (
            patch.object(batch, "get_job", return_value=job),
            patch.object(batch, "patch_job") as progress,
            patch.object(batch, "_load_lands", return_value=make_lands("S1")[:1]),
            patch.object(batch, "search_s1_scenes", return_value=[scene]) as search,
            patch.object(
                batch, "_download_scene", side_effect=RuntimeError("read failed")
            ),
        ):
            result = batch.process_satellite_batch.run("job")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(progress.call_args.args[1]["status"], "failed")
        self.assertFalse(search.call_args.kwargs["dedupe_week"])

    def test_completed_job_does_not_download_again(self):
        with (
            patch.object(batch, "get_job", return_value={"status": "completed"}),
            patch.object(batch, "_load_lands") as lands,
        ):
            result = batch.process_satellite_batch.run("job")
        self.assertEqual(result["status"], "already_handled")
        lands.assert_not_called()

    def test_categorical_crop_preserves_classes_and_source(self):
        from rasterio.transform import from_origin

        source = np.array([[4, 9], [4, 9]], dtype=np.float32)
        result = batch.crop_shared_array(
            source,
            from_origin(0, 2, 1, 1),
            (4, 4),
            from_origin(0, 2, 0.5, 0.5),
            categorical=True,
        )
        self.assertEqual(set(np.unique(result)), {4, 9})
        result[0, 0] = np.nan
        self.assertEqual(source[0, 0], 4)


class ParcelProductTests(unittest.TestCase):
    def setUp(self):
        self.lands = make_lands()
        union = unary_union([land["geom"] for land in self.lands])
        self.grid = compute_target_grid(union.bounds, union)
        rows, cols = np.indices(self.grid[1])
        longitudes, _ = xy(self.grid[0], rows, cols)
        self.left = np.asarray(longitudes).reshape(self.grid[1]) < 110.006

    def test_optical_pixels_and_statistics_are_isolated(self):
        keys = {
            key
            for definition in batch.agri_optical_index_defs()
            for key in definition.bands
        }
        bands = {key: np.full(self.grid[1], 0.2, dtype=np.float32) for key in keys}
        bands["B08"][self.left] = 0.6
        original = bands["B08"].copy()
        scene = {"id": "scene", "date": date(2026, 8, 1), "cloud_cover": 0}
        products = []

        def publish(row, **kwargs):
            products.append((row, kwargs["mq_task_id"]))
            return "oss-url"

        with (
            patch.object(batch, "upload_field_rgb_preview", return_value={}),
            patch.object(batch, "decloud_enabled", return_value=False),
            patch(
                "app.tasks.agri_lonlat.publish_optical_lonlat_to_oss_mq",
                side_effect=publish,
            ),
        ):
            for land in self.lands:
                self.assertTrue(
                    batch._publish_land(
                        scene, "S2", land, bands, None, self.grid, "parent"
                    )
                )
        self.assertAlmostEqual(products[0][0]["ndvi_avg"], 0.5, places=5)
        self.assertAlmostEqual(products[1][0]["ndvi_avg"], 0, places=5)
        self.assertEqual([parent for _, parent in products], ["parent:A", "parent:B"])
        self.assertEqual(
            [kwargs["result_delivery"] for _, kwargs in products],
            ["http", "http"],
        )
        for (row, _), land in zip(products, self.lands):
            for pixel in row["_pixel_data_obj"]["pixels"]:
                self.assertTrue(land["geom"].covers(Point(pixel["lon"], pixel["lat"])))
        np.testing.assert_array_equal(bands["B08"], original)

    def test_s1_statistics_do_not_include_neighbor(self):
        bands = {
            "vv": np.where(self.left, -10.0, -20.0).astype(np.float32),
            "vh": np.full(self.grid[1], -25.0, dtype=np.float32),
        }
        original = bands["vv"].copy()
        scene = {"id": "S1", "date": date(2026, 8, 1)}
        with patch.object(batch, "_upsert_agri_s1", return_value="oss") as publish:
            for land in self.lands:
                self.assertTrue(
                    batch._publish_land(
                        scene, "S1", land, bands, None, self.grid, "parent"
                    )
                )
        self.assertAlmostEqual(publish.call_args_list[0].args[6]["mean"], -10.0)
        self.assertAlmostEqual(publish.call_args_list[1].args[6]["mean"], -20.0)
        self.assertEqual(
            [call.kwargs["mq_task_id"] for call in publish.call_args_list],
            ["parent:A", "parent:B"],
        )
        self.assertEqual(
            [call.kwargs["result_delivery"] for call in publish.call_args_list],
            ["http", "http"],
        )
        np.testing.assert_array_equal(bands["vv"], original)


if __name__ == "__main__":
    unittest.main()
