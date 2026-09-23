"""虚拟项目区任务编排的幂等时间窗口和精确边界测试。"""

import uuid
from datetime import date
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock

from app.services.virtual_area_service import (
    create_vpa10_download_jobs,
    default_history_window,
)


class VirtualAreaServiceTests(IsolatedAsyncioTestCase):
    def test_history_window_handles_leap_day(self) -> None:
        self.assertEqual(
            default_history_window(as_of=date(2024, 2, 29), years=5),
            (date(2019, 2, 28), date(2024, 2, 29)),
        )

    async def test_download_jobs_keep_dynamic_boundary_and_use_area_as_unit(
        self,
    ) -> None:
        db = MagicMock()
        parent_id = uuid.uuid4()
        area_boundary = {
            "type": "Polygon",
            "coordinates": [
                [[110.0, 35.0], [110.1, 35.0], [110.1, 35.1], [110.0, 35.0]]
            ],
        }
        jobs = await create_vpa10_download_jobs(
            db,
            [
                {
                    "tile_id": "vpa10_demo",
                    "anchor_land_id": "anchor",
                    "land_ids": ["anchor", "B"],
                    "boundary_geojson": area_boundary,
                    "min_lon": 110.0,
                    "min_lat": 35.0,
                    "max_lon": 110.1,
                    "max_lat": 35.1,
                    "source_properties": {},
                }
            ],
            date_from=date(2024, 1, 1),
            date_to=date(2024, 1, 3),
            sensors=("S1", "S2"),
            parent_job_id=parent_id,
            chunk_days=2,
        )

        self.assertEqual(len(jobs), 4)
        self.assertEqual(db.add_all.call_count, 1)
        for job in jobs:
            self.assertEqual(job.params_json["land_ids"], ["anchor", "B"])
            self.assertEqual(job.params_json["processing_window_km"], 10.0)
            self.assertEqual(
                job.params_json["processing_boundary_geojson"], area_boundary
            )


if __name__ == "__main__":
    import unittest

    unittest.main()
