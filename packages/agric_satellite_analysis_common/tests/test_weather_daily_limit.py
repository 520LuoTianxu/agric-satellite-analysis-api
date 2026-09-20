"""Tests for the download-host local weather API quota marker."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from agric_satellite_analysis_common import weather_daily_limit as limit


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def exists(self, key: str) -> int:
        return int(key in self.values)

    def set(self, key: str, value: str, *, ex: int) -> bool:
        self.values[key] = value
        self.ttls[key] = ex
        return True


class WeatherDailyLimitTests(unittest.TestCase):
    def test_marker_is_scoped_to_business_day_and_expires_at_midnight(self) -> None:
        fake = _FakeRedis()
        now = datetime(2026, 9, 20, 12, 0, tzinfo=limit.LOCAL_TIMEZONE)
        tomorrow = datetime(2026, 9, 21, 0, 1, tzinfo=limit.LOCAL_TIMEZONE)

        with patch.object(limit, "_get_client", return_value=fake):
            self.assertTrue(limit.mark_weather_daily_limit_reached(now))
            self.assertTrue(limit.weather_daily_limit_reached(now))
            self.assertFalse(limit.weather_daily_limit_reached(tomorrow))

        key = limit.daily_limit_key(now)
        self.assertEqual(fake.ttls[key], 12 * 60 * 60)


if __name__ == "__main__":
    unittest.main()
