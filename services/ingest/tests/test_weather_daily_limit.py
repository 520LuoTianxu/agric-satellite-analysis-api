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

    def test_daily_limit_marks_machine_and_skips_retry(self) -> None:
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
        ):
            result = wx.fetch_weather_for_land.run("L1", backfill_days=1)

        mark.assert_called_once_with()
        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(result["error"], DAILY_API_LIMIT_MESSAGE)

    def test_already_limited_machine_skips_weather_request(self) -> None:
        with (
            patch.object(wx, "weather_daily_limit_reached", return_value=True),
            patch.object(wx, "_fetch_open_meteo") as fetch,
        ):
            result = wx.fetch_weather_for_land.run("L1")

        fetch.assert_not_called()
        self.assertEqual(result["reason"], "daily_api_limit_reached")

    def test_daily_schedule_skips_when_machine_is_limited(self) -> None:
        with (
            patch.object(wx, "weather_daily_limit_reached", return_value=True),
            patch.object(wx, "group") as task_group,
        ):
            result = wx.schedule_daily_weather_fetch.run()

        task_group.assert_not_called()
        self.assertEqual(result["reason"], "daily_api_limit_reached")


if __name__ == "__main__":
    unittest.main()
