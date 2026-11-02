"""Weather API daily quota handling tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from agric_satellite_analysis_common.weather_daily_limit import (
    DAILY_API_LIMIT_MESSAGE,
)
from app.tasks import weather as wx


class WeatherDailyLimitTaskTests(unittest.TestCase):
    def test_daily_limit_response_requires_429_and_expected_message(self) -> None:
        request = httpx.Request("GET", "https://api.open-meteo.com/v1/forecast")
        matching = httpx.Response(
            429,
            text=DAILY_API_LIMIT_MESSAGE,
            request=request,
        )
        other_message = httpx.Response(429, text="temporary throttling", request=request)
        other_status = httpx.Response(
            500,
            text=DAILY_API_LIMIT_MESSAGE,
            request=request,
        )

        self.assertTrue(wx._is_daily_api_limit_response(matching))
        self.assertFalse(wx._is_daily_api_limit_response(other_message))
        self.assertFalse(wx._is_daily_api_limit_response(other_status))

    def test_daily_limit_marks_machine_then_falls_back_to_nasa(self) -> None:
        request = httpx.Request("GET", "https://api.open-meteo.com/v1/forecast")
        response = httpx.Response(
            429,
            text=DAILY_API_LIMIT_MESSAGE,
            request=request,
        )
        error = httpx.HTTPStatusError(
            "weather API returned 429",
            request=request,
            response=response,
        )
        nasa_daily = {
            "daily": {
                "time": ["2026-11-01"],
                "temperature_2m_min": [1.0],
                "temperature_2m_max": [9.0],
                "temperature_2m_mean": [5.0],
                "precipitation_sum": [0.0],
                "shortwave_radiation_sum": [6.0],
                "wind_speed_10m_max": [2.0],
                "soil_temperature_0cm": [1.0],
                "soil_temperature_6cm": [1.0],
                "soil_temperature_18cm": [1.0],
                "soil_temperature_54cm": [1.0],
                "soil_moisture_0_to_1cm": [0.2],
                "vapour_pressure_deficit": [0.3],
                "cloud_cover": [10.0],
            },
            "provider": "nasa-power",
        }

        with (
            patch.object(wx, "weather_daily_limit_reached", return_value=False),
            patch.object(
                wx, "mark_weather_daily_limit_reached", return_value=True
            ) as mark,
            patch.object(wx, "_http_only_weather", return_value=True),
            patch(
                "agric_satellite_analysis_common.internal_api.resolve_land",
                return_value={"base_id": None, "land_area_mu": None},
            ),
            patch.object(wx, "_resolve_land_lat_lon", return_value=(1.0, 2.0)),
            patch.object(wx, "_fetch_open_meteo", side_effect=error),
            patch.object(wx, "_fetch_nasa_power", return_value=nasa_daily),
            patch(
                "agric_satellite_analysis_common.internal_api.http_writes_enabled",
                return_value=False,
            ),
        ):
            result = wx.fetch_weather_for_land.run("L1", backfill_days=1)

        mark.assert_called_once_with()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["source"], "nasa-power")
        self.assertEqual(result["fallback_reason"], "daily_api_limit_reached")

    def test_already_limited_machine_skips_open_meteo_uses_nasa(self) -> None:
        nasa_daily = {
            "daily": {
                "time": ["2026-11-01"],
                "temperature_2m_min": [0.0],
                "temperature_2m_max": [8.0],
                "temperature_2m_mean": [4.0],
                "precipitation_sum": [0.0],
                "shortwave_radiation_sum": [7.0],
                "wind_speed_10m_max": [2.0],
                "soil_temperature_0cm": [1.0],
                "soil_temperature_6cm": [1.0],
                "soil_temperature_18cm": [1.0],
                "soil_temperature_54cm": [1.0],
                "soil_moisture_0_to_1cm": [0.2],
                "vapour_pressure_deficit": [0.4],
                "cloud_cover": [20.0],
            },
            "provider": "nasa-power",
        }
        with (
            patch.object(wx, "weather_daily_limit_reached", return_value=True),
            patch.object(wx, "_http_only_weather", return_value=True),
            patch(
                "agric_satellite_analysis_common.internal_api.resolve_land",
                return_value={"base_id": None, "land_area_mu": None},
            ),
            patch.object(wx, "_resolve_land_lat_lon", return_value=(1.0, 2.0)),
            patch.object(wx, "_fetch_open_meteo") as fetch,
            patch.object(wx, "_fetch_nasa_power", return_value=nasa_daily),
            patch(
                "agric_satellite_analysis_common.internal_api.http_writes_enabled",
                return_value=False,
            ),
        ):
            result = wx.fetch_weather_for_land.run("L1", backfill_days=1)

        fetch.assert_not_called()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["source"], "nasa-power")
        self.assertEqual(result["fallback_reason"], "daily_api_limit_reached")

    def test_daily_schedule_still_dispatches_when_machine_is_limited(self) -> None:
        fake_group = patch.object(wx, "group")
        with (
            patch.object(wx, "weather_daily_limit_reached", return_value=True),
            fake_group as task_group,
            patch(
                "agric_satellite_analysis_common.internal_api.internal_api_enabled",
                return_value=True,
            ),
            patch(
                "agric_satellite_analysis_common.internal_api.weather_land_ids",
                return_value={"land_ids": ["L1", "L2"], "batch_size": 50},
            ),
        ):
            task_group.return_value.apply_async = lambda: None
            # group() is called once per batch; make it return an object with apply_async
            class _G:
                def apply_async(self_inner):
                    return None

            task_group.side_effect = lambda *a, **k: _G()
            result = wx.schedule_daily_weather_fetch.run()

        self.assertTrue(result.get("open_meteo_limited"))
        self.assertEqual(result["lands"], 2)
        self.assertGreaterEqual(result["batches"], 1)


if __name__ == "__main__":
    unittest.main()
