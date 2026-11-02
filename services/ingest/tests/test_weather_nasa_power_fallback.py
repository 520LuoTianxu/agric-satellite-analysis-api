"""NASA POWER weather fallback parsing and trigger tests."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import httpx
from agric_satellite_analysis_common.weather_daily_limit import (
    DAILY_API_LIMIT_MESSAGE,
)
from app.tasks import weather as wx


def _sample_nasa_payload() -> dict:
    """Minimal POWER daily JSON shaped like the public API."""
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [116.4, 39.9, 200.0]},
        "properties": {
            "parameter": {
                "T2M": {"20240101": -4.5, "20240102": -3.2},
                "T2M_MAX": {"20240101": 2.8, "20240102": 5.0},
                "T2M_MIN": {"20240101": -9.6, "20240102": -8.5},
                "PRECTOTCORR": {"20240101": 0.0, "20240102": 1.2},
                "ALLSKY_SFC_SW_DWN": {"20240101": 5.05, "20240102": 5.28},
                "WS10M_MAX": {"20240101": 3.15, "20240102": 6.02},
                "CLOUD_AMT": {"20240101": 40.0, "20240102": 55.0},
                "TS": {"20240101": -6.0, "20240102": -4.0},
                "TSOIL1": {"20240101": -5.0, "20240102": -3.5},
                "TSOIL2": {"20240101": -4.0, "20240102": -2.5},
                "TSOIL3": {"20240101": -3.0, "20240102": -1.5},
                "SFMC": {"20240101": 0.25, "20240102": 0.26},
                "T2MDEW": {"20240101": -8.0, "20240102": -9.0},
                "RH2M": {"20240101": 70.0, "20240102": 60.0},
            }
        },
        "header": {"fill_value": -999.0, "start": "20240101", "end": "20240102"},
        "messages": [],
    }


class NasaPowerParseTests(unittest.TestCase):
    def test_parse_maps_core_fields_and_leaves_et0_null(self) -> None:
        with patch("app.tasks.weather.httpx.Client") as client_cls:
            response = MagicMock()
            response.raise_for_status = MagicMock()
            response.json.return_value = _sample_nasa_payload()
            client = MagicMock()
            client.__enter__.return_value = client
            client.__exit__.return_value = False
            client.get.return_value = response
            client_cls.return_value = client

            data = wx._fetch_nasa_power(39.9, 116.4, date(2024, 1, 1), date(2024, 1, 2))

        daily = data["daily"]
        self.assertEqual(daily["time"], ["2024-01-01", "2024-01-02"])
        self.assertEqual(daily["temperature_2m_min"][0], -9.6)
        self.assertEqual(daily["temperature_2m_max"][0], 2.8)
        self.assertEqual(daily["temperature_2m_mean"][0], -4.5)
        self.assertEqual(daily["precipitation_sum"][1], 1.2)
        self.assertEqual(daily["shortwave_radiation_sum"][0], 5.05)
        self.assertEqual(daily["wind_speed_10m_max"][1], 6.02)
        self.assertEqual(daily["cloud_cover"][0], 40.0)
        self.assertEqual(daily["soil_temperature_0cm"][0], -6.0)
        self.assertEqual(daily["soil_moisture_0_to_1cm"][0], 0.25)
        self.assertIsNotNone(daily["vapour_pressure_deficit"][0])
        self.assertNotIn("et0_fao_evapotranspiration", daily)
        self.assertNotIn("soil_moisture_1_to_3cm", daily)
        self.assertEqual(data["provider"], "nasa-power")

        # Request should hit configured POWER URL with AG community.
        args, kwargs = client.get.call_args
        self.assertIn("power.larc.nasa.gov", args[0])
        self.assertEqual(kwargs["params"]["community"], "AG")
        self.assertEqual(kwargs["params"]["start"], "20240101")

    def test_fill_value_becomes_none(self) -> None:
        payload = _sample_nasa_payload()
        payload["properties"]["parameter"]["T2M"]["20240101"] = -999.0
        with patch("app.tasks.weather.httpx.Client") as client_cls:
            response = MagicMock()
            response.raise_for_status = MagicMock()
            response.json.return_value = payload
            client = MagicMock()
            client.__enter__.return_value = client
            client.__exit__.return_value = False
            client.get.return_value = response
            client_cls.return_value = client
            data = wx._fetch_nasa_power(39.9, 116.4, date(2024, 1, 1), date(2024, 1, 2))
        self.assertIsNone(data["daily"]["temperature_2m_mean"][0])


class NasaPowerFallbackTriggerTests(unittest.TestCase):
    def test_fallback_on_open_meteo_503(self) -> None:
        request = httpx.Request("GET", "https://archive-api.open-meteo.com/v1/archive")
        response = httpx.Response(503, text="unavailable", request=request)
        error = httpx.HTTPStatusError("503", request=request, response=response)
        nasa_daily = {
            "daily": {
                "time": ["2026-11-01"],
                "temperature_2m_min": [1.0],
                "temperature_2m_max": [10.0],
                "temperature_2m_mean": [5.0],
                "precipitation_sum": [0.0],
                "shortwave_radiation_sum": [8.0],
                "wind_speed_10m_max": [3.0],
                "soil_temperature_0cm": [2.0],
                "soil_temperature_6cm": [2.1],
                "soil_temperature_18cm": [2.2],
                "soil_temperature_54cm": [2.3],
                "soil_moisture_0_to_1cm": [0.2],
                "vapour_pressure_deficit": [0.5],
                "cloud_cover": [30.0],
            },
            "provider": "nasa-power",
        }

        with patch.object(wx, "weather_daily_limit_reached", return_value=False):
            with patch.object(wx, "_http_only_weather", return_value=True):
                with patch(
                    "agric_satellite_analysis_common.internal_api.resolve_land",
                    return_value={"base_id": None, "land_area_mu": None},
                ):
                    with patch.object(
                        wx, "_resolve_land_lat_lon", return_value=(39.9, 116.4)
                    ):
                        with patch(
                            "agric_satellite_analysis_common.internal_api.http_writes_enabled",
                            return_value=False,
                        ):
                            with patch.object(
                                wx, "_fetch_open_meteo", side_effect=error
                            ):
                                with patch.object(
                                    wx,
                                    "_fetch_nasa_power",
                                    return_value=nasa_daily,
                                ) as nasa:
                                    with patch.object(wx.logger, "info") as log_info:
                                        result = wx.fetch_weather_for_land.run(
                                            "L1", backfill_days=1
                                        )

        nasa.assert_called_once()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["source"], "nasa-power")
        self.assertEqual(result["fallback_reason"], "http_503")
        self.assertEqual(result["rows_upserted"], 1)
        fallback_events = [
            c
            for c in log_info.call_args_list
            if c.args and c.args[0] == "weather_fetch_fallback_nasa_power"
        ]
        self.assertTrue(fallback_events)
        self.assertEqual(fallback_events[0].kwargs.get("reason"), "http_503")

    def test_fallback_when_machine_already_daily_limited(self) -> None:
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
        with patch.object(wx, "weather_daily_limit_reached", return_value=True):
            with patch.object(wx, "_http_only_weather", return_value=True):
                with patch(
                    "agric_satellite_analysis_common.internal_api.resolve_land",
                    return_value={"base_id": None, "land_area_mu": None},
                ):
                    with patch.object(
                        wx, "_resolve_land_lat_lon", return_value=(39.9, 116.4)
                    ):
                        with patch(
                            "agric_satellite_analysis_common.internal_api.http_writes_enabled",
                            return_value=False,
                        ):
                            with patch.object(wx, "_fetch_open_meteo") as om:
                                with patch.object(
                                    wx,
                                    "_fetch_nasa_power",
                                    return_value=nasa_daily,
                                ) as nasa:
                                    result = wx.fetch_weather_for_land.run(
                                        "L1", backfill_days=1
                                    )

        om.assert_not_called()
        nasa.assert_called_once()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["source"], "nasa-power")
        self.assertEqual(result["fallback_reason"], "daily_api_limit_reached")

    def test_fallback_on_open_meteo_429_daily_limit(self) -> None:
        request = httpx.Request("GET", "https://api.open-meteo.com/v1/forecast")
        response = httpx.Response(
            429, text=DAILY_API_LIMIT_MESSAGE, request=request
        )
        error = httpx.HTTPStatusError("429", request=request, response=response)
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
        with patch.object(wx, "weather_daily_limit_reached", return_value=False):
            with patch.object(
                wx, "mark_weather_daily_limit_reached", return_value=True
            ) as mark:
                with patch.object(wx, "_http_only_weather", return_value=True):
                    with patch(
                        "agric_satellite_analysis_common.internal_api.resolve_land",
                        return_value={"base_id": None, "land_area_mu": None},
                    ):
                        with patch.object(
                            wx, "_resolve_land_lat_lon", return_value=(39.9, 116.4)
                        ):
                            with patch(
                                "agric_satellite_analysis_common.internal_api.http_writes_enabled",
                                return_value=False,
                            ):
                                with patch.object(
                                    wx, "_fetch_open_meteo", side_effect=error
                                ):
                                    with patch.object(
                                        wx,
                                        "_fetch_nasa_power",
                                        return_value=nasa_daily,
                                    ):
                                        result = wx.fetch_weather_for_land.run(
                                            "L1", backfill_days=1
                                        )

        mark.assert_called_once_with()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["source"], "nasa-power")
        self.assertEqual(result["fallback_reason"], "daily_api_limit_reached")

    def test_fallback_on_empty_open_meteo_daily(self) -> None:
        nasa_daily = {
            "daily": {
                "time": ["2026-11-01"],
                "temperature_2m_min": [2.0],
                "temperature_2m_max": [11.0],
                "temperature_2m_mean": [6.0],
                "precipitation_sum": [0.0],
                "shortwave_radiation_sum": [9.0],
                "wind_speed_10m_max": [4.0],
                "soil_temperature_0cm": [3.0],
                "soil_temperature_6cm": [3.0],
                "soil_temperature_18cm": [3.0],
                "soil_temperature_54cm": [3.0],
                "soil_moisture_0_to_1cm": [0.2],
                "vapour_pressure_deficit": [0.6],
                "cloud_cover": [5.0],
            },
            "provider": "nasa-power",
        }
        with patch.object(wx, "weather_daily_limit_reached", return_value=False):
            with patch.object(wx, "_http_only_weather", return_value=True):
                with patch(
                    "agric_satellite_analysis_common.internal_api.resolve_land",
                    return_value={"base_id": None, "land_area_mu": None},
                ):
                    with patch.object(
                        wx, "_resolve_land_lat_lon", return_value=(39.9, 116.4)
                    ):
                        with patch(
                            "agric_satellite_analysis_common.internal_api.http_writes_enabled",
                            return_value=False,
                        ):
                            with patch.object(
                                wx,
                                "_fetch_open_meteo",
                                return_value={"daily": {"time": []}},
                            ):
                                with patch.object(
                                    wx,
                                    "_fetch_nasa_power",
                                    return_value=nasa_daily,
                                ):
                                    result = wx.fetch_weather_for_land.run(
                                        "L1", backfill_days=1
                                    )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["fallback_reason"], "no_data")
        self.assertEqual(result["source"], "nasa-power")


if __name__ == "__main__":
    unittest.main()
