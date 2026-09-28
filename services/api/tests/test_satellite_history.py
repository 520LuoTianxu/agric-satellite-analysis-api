"""历史回填日期窗口和临时 10km Job 规划回归测试。"""

import uuid
from datetime import date
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock

from pyproj import CRS, Transformer
from shapely.geometry import box, mapping
from shapely.ops import transform

from app.services.satellite_batch import create_satellite_batch_jobs
from app.services.satellite_history import default_history_window


def local_land(land_id: str, x: float = 0):
    local = CRS.from_proj4("+proj=aeqd +lat_0=35 +lon_0=110 +datum=WGS84 +units=m")
    to_wgs = Transformer.from_crs(local, 4326, always_xy=True)
    geometry = transform(to_wgs.transform, box(x - 50, -50, x + 50, 50))
    return SimpleNamespace(
        land_id=land_id,
        boundary_geojson=mapping(geometry),
        boundary_srid=4326,
    )


class SatelliteHistoryTests(IsolatedAsyncioTestCase):
    def test_history_window_handles_leap_day(self) -> None:
        self.assertEqual(
            default_history_window(as_of=date(2024, 2, 29), years=5),
            (date(2019, 2, 28), date(2024, 2, 29)),
        )

    async def test_dynamic_window_jobs_are_transient_and_carry_exact_boundary(self) -> None:
        db = MagicMock()
        parent_id = uuid.uuid4()
        groups, jobs, metadata = await create_satellite_batch_jobs(
            db,
            [local_land("anchor"), local_land("nearby", x=8_500)],
            date_from=date(2024, 1, 1),
            date_to=date(2024, 1, 3),
            sensors=("S1", "S2"),
            parent_job_id=parent_id,
            chunk_days=2,
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0].land_ids), {"anchor", "nearby"})
        self.assertEqual(len(jobs), 4)
        self.assertEqual(db.add_all.call_count, 1)
        self.assertEqual(metadata["window_side_m"], 10_000)
        for job in jobs:
            self.assertEqual(job.params_json["land_ids"], groups[0].land_ids)
            self.assertEqual(job.params_json["processing_window_km"], 10)
            self.assertEqual(
                job.params_json["processing_boundary_geojson"],
                groups[0].processing_boundary_geojson,
            )
            self.assertNotIn("virtual_area_tile_id", job.params_json)
            self.assertNotIn("virtual_area", job.params_json)


if __name__ == "__main__":
    import unittest

    unittest.main()
