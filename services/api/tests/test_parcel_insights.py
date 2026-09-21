"""历史报告不能混入近期数据；匹配、缺失、对照与快照须保留真实口径。"""

import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.database import get_db
from app.routers.parcel_insights import router
from app.schemas.parcel_insights import InsightsRequest
from app.services.parcel_insights import (
    build_insights,
    event_review,
    matched_comparison,
    official_points,
    shifted,
)


def point(day, value=0.6):
    return {
        "date": day,
        "ndvi": value,
        "ndvi_avg": value,
        "ndmi": 0.2,
        "official": True,
        "scene_id": day,
    }


def request(**overrides):
    return InsightsRequest(
        **{
            "land_ids": ["A"],
            "start_date": "2025-01-01",
            "end_date": "2025-12-31",
            **overrides,
        }
    )


class AnalysisTests(unittest.TestCase):
    def test_three_lands_cannot_span_six_days_around_anchor(self):
        result = matched_comparison(
            {
                "A": [point("2025-06-10")],
                "B": [point("2025-06-07")],
                "C": [point("2025-06-13")],
            }
        )
        self.assertEqual(result["count"], 0)

    def test_valid_product_selection_deduplicates_days(self):
        base = {
            "date": "2025-06-01",
            "ndvi_avg": 0.7,
            "parcel_cloud_cover_pct": 10,
            "cloud_cover": 10,
            "source": "stac_direct",
            "scene_id": "raw",
        }
        raw = official_points(
            [
                base,
                {
                    **base,
                    "source": "uncrtaints_decloud",
                    "decloud_quality": "good",
                    "scene_id": "decloud",
                    "ndvi_avg": 0.4,
                },
            ]
        )
        self.assertEqual(len(raw), 1)
        self.assertEqual(raw[0]["ndvi"], 0.7)
        self.assertFalse(
            official_points([{**base, "parcel_cloud_cover_pct": 90, "cloud_cover": 90}])
        )
        self.assertFalse(
            official_points(
                [{**base, "source": "uncrtaints_decloud", "decloud_quality": "bad"}]
            )
        )

    def test_near_dates_require_three_unique_pairs(self):
        series = {
            "A": [point("2025-06-01"), point("2025-06-02"), point("2025-06-03")],
            "B": [point("2025-06-02", 0.4)],
        }
        result = matched_comparison(series)
        self.assertEqual(result["count"], 1)
        self.assertFalse(result["means"])
        series = {
            "A": [point(f"2025-06-{day:02}") for day in [1, 10, 20]],
            "B": [point(f"2025-06-{day:02}", 0.4) for day in [2, 11, 21]],
        }
        self.assertEqual(matched_comparison(series)["differences"]["B"], -0.2)

    def test_reference_dates_support_leap_day(self):
        self.assertEqual(shifted(date(2024, 2, 29), 2023), date(2023, 2, 28))
        result = matched_comparison(
            {
                "selected": [point(f"2025-06-{d:02}") for d in [1, 10, 20]],
                "reference": [point(f"2024-06-{d:02}", 0.5) for d in [1, 10, 20]],
            },
            shift_years=1,
        )
        self.assertEqual(result["count"], 3)

    def test_request_rejects_today_in_reports_and_invalid_windows(self):
        with self.assertRaises(ValidationError):
            request(start_date=date.today() - timedelta(days=30), end_date=date.today())
        with self.assertRaises(ValidationError):
            request(land_ids=["A", "A"])
        with self.assertRaises(ValidationError):
            request(reference_year=2025)
        with self.assertRaises(ValidationError):
            request(
                seasons={"B": {"start_date": "2025-01-01", "end_date": "2025-12-31"}}
            )
        self.assertEqual(
            request(
                mode="recent",
                end_date=date.today(),
                start_date=date.today() - timedelta(days=30),
            ).mode,
            "recent",
        )

    def test_action_change_and_control_missingness(self):
        series = {
            "A": [
                point(f"2025-06-{d:02}", 0.4 if d < 15 else 0.7) for d in [1, 8, 20, 28]
            ],
            "B": [],
        }
        event = {
            "land_id": "A",
            "date": "2025-06-15",
            "action": "灌溉",
            "note": "现场记录",
            "window_days": 30,
            "control_land_id": "B",
        }
        result = event_review(event, series)
        self.assertEqual(result["change"], 0.3)
        self.assertIsNone(result["relative_change"])
        self.assertEqual(result["control"]["before"]["count"], 0)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_cross_year_window_limits_observations_rain_and_reference(
        self,
    ):
        land = SimpleNamespace(
            land_id="A",
            land_name="A",
            crop_type="小麦",
            group_name="项目",
            land_area_mu=100,
            area_ha=None,
            boundary_geojson={},
        )
        db = AsyncMock()
        first, second = MagicMock(), MagicMock()
        first.scalars.return_value.all.return_value = [land]
        second.all.return_value = [
            ("A", date(2025, 1, 5), 2),
            ("A", date(2025, 3, 5), 99),
        ]
        db.execute.side_effect = [first, second]
        points = [
            point("2024-01-05", 0.4),
            point("2024-03-05", 0.8),
            point("2024-12-10", 0.8),
            point("2025-01-05", 0.5),
            point("2025-03-05", 0.9),
            point("2025-05-05", 1),
        ]
        body = request(
            start_date="2024-11-01",
            end_date="2025-04-01",
            reference_year=2023,
            seasons={
                "A": {
                    "start_date": "2025-01-01",
                    "end_date": "2025-02-28",
                    "crop": "冬小麦",
                }
            },
        )
        with (
            patch(
                "app.services.parcel_insights.load_points",
                AsyncMock(return_value={"A": points}),
            ),
            patch(
                "app.services.parcel_insights.spatial_snapshot",
                AsyncMock(return_value=None),
            ),
        ):
            item = (await build_insights(db, body))["items"][0]
        self.assertEqual(item["summary"]["mean_ndvi"], 0.5)
        self.assertEqual(
            item["rainfall"], {"mm": 2, "observed_days": 1, "period_days": 59}
        )
        self.assertEqual(item["history"]["summary"]["mean_ndvi"], 0.4)
        self.assertEqual(item["history"]["start_date"], "2024-01-01")
        self.assertEqual(item["crop"], "冬小麦")

    async def test_missing_land_data_does_not_become_zero_or_healthy(self):
        land = SimpleNamespace(
            land_id="A",
            land_name="A",
            crop_type=None,
            group_name="项目",
            land_area_mu=None,
            area_ha=None,
            boundary_geojson={},
        )
        db = AsyncMock()
        first, second = MagicMock(), MagicMock()
        first.scalars.return_value.all.return_value = [land]
        second.all.return_value = []
        db.execute.side_effect = [first, second]
        with patch(
            "app.services.parcel_insights.load_points",
            AsyncMock(return_value={"A": []}),
        ):
            result = await build_insights(db, request())
        item = result["items"][0]
        self.assertIsNone(item["summary"]["mean_ndvi"])
        self.assertIsNone(item["area_mu"])
        self.assertIsNone(item["rainfall"]["mm"])
        self.assertEqual(item["progress"], "unknown")


class SnapshotApiTests(unittest.TestCase):
    def setUp(self):
        self.db = AsyncMock()
        self.db.add = MagicMock()
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def test_historical_result_is_saved_and_recent_result_is_not(self):
        result = {"request": request().model_dump(mode="json"), "items": []}
        with patch(
            "app.routers.parcel_insights.build_insights", AsyncMock(return_value=result)
        ):
            response = self.client.post(
                "/parcel-insights", json=request().model_dump(mode="json")
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["snapshot_id"])
        self.assertEqual(
            self.db.add.call_args.args[0].params_json["snapshot"], response.json()
        )
        self.db.add.reset_mock()
        with patch(
            "app.routers.parcel_insights.build_insights",
            AsyncMock(return_value={"request": {"mode": "recent"}}),
        ):
            response = self.client.post(
                "/parcel-insights", json=request(mode="recent").model_dump(mode="json")
            )
        self.assertIsNone(response.json()["snapshot_id"])
        self.db.add.assert_not_called()

    def test_download_uses_saved_snapshot_without_reanalysis(self):
        snapshot = {
            "request": {"mode": "historical"},
            "items": [{"land_name": "已保存地块"}],
        }
        with (
            patch(
                "app.routers.parcel_insights.read_snapshot",
                AsyncMock(return_value=snapshot),
            ),
            patch(
                "app.reports.parcel_insights.render_report",
                return_value=b"%PDF-fixture",
            ) as render,
            patch("app.routers.parcel_insights.build_insights", AsyncMock()) as build,
        ):
            response = self.client.get(f"/parcel-insights/{uuid4()}/report.pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"%PDF-fixture")
        render.assert_called_once_with(snapshot)
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
