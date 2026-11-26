"""Official download-machine weather fetch, context maps, and generate entry."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import numpy as np


def _tiny_png(path: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(2, 1.2), dpi=80)
    ax.imshow(np.linspace(0, 1, 24).reshape(4, 6), cmap="RdYlGn")
    ax.set_axis_off()
    fig.savefig(path, dpi=80, facecolor="white")
    plt.close(fig)
    return path


def _synthetic_product(
    day: str,
    *,
    metric: str = "NDRE",
    offset: float = 0.0,
    n: int = 36,
    stac_item_id: str = "S2C_49SEB_20260409_0_L2A",
) -> dict:
    rows, cols = 6, 6
    lons = []
    lats = []
    values = []
    for r in range(rows):
        for c in range(cols):
            lons.append(112.084 + c * 0.0004)
            lats.append(37.253 - r * 0.00025)
            values.append(0.04 + 0.01 * c - 0.008 * r + offset)
    return {
        "land_id": "19994",
        "date": day,
        "sensor": "S2",
        "parcel_cloud_cover_pct": 0.0,
        "cloud_cover": 2.0,
        "stac_item_id": stac_item_id,
        "processing_window": {
            "bounds": [112.029821, 37.207627, 112.142607, 37.297732],
        },
        "pixel_data": {
            "format": "lonlat_columns_v1",
            "columns": {"lon": lons, "lat": lats, metric: values},
        },
    }


class WeatherFetchTests(unittest.TestCase):
    def test_rows_from_daily_payload_and_summary(self):
        from app.reports.biweekly.weather_fetch import rows_from_daily_payload, summarize_weather_rows

        rows = rows_from_daily_payload(
            {
                "time": ["2026-04-02", "2026-04-03", "2026-04-04"],
                "precipitation_sum": [1.5, 0.0, None],
                "temperature_2m_min": [4.0, 5.0, 6.0],
                "temperature_2m_max": [14.0, 16.0, 18.0],
                "et0_fao_evapotranspiration": [2.0, 2.5, 3.0],
            }
        )
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["precipitation_mm"], 1.5)
        self.assertIsNone(rows[2]["precipitation_mm"])
        summary = summarize_weather_rows(rows)
        self.assertEqual(summary["days"], 3)
        self.assertEqual(summary["precipitation_mm"], 1.5)
        self.assertEqual(summary["et0_mm"], 7.5)
        self.assertEqual(summary["min_c"], 4.0)
        self.assertEqual(summary["max_c"], 18.0)
        self.assertTrue(summary["missing_precipitation"])

    def test_fetch_period_weather_uses_open_meteo_and_splits_windows(self):
        from app.reports.biweekly.weather_fetch import fetch_period_weather

        daily = {
            "time": [f"2026-03-{d:02d}" for d in range(3, 32)]
            + [f"2026-04-{d:02d}" for d in range(1, 16)],
            "precipitation_sum": [0.0] * 29 + [1.0] * 15,
            "temperature_2m_min": [2.0] * 44,
            "temperature_2m_max": [12.0] * 44,
            "et0_fao_evapotranspiration": [2.0] * 44,
        }

        def _open(_lat, _lon, start, end, *, use_archive=False):
            self.assertTrue(use_archive)
            if start.year == 2025:
                return {
                    "daily": {
                        "time": ["2025-04-02", "2025-04-15"],
                        "precipitation_sum": [0.0, 0.0],
                        "temperature_2m_min": [1.0, 1.0],
                        "temperature_2m_max": [9.0, 9.0],
                        "et0_fao_evapotranspiration": [1.0, 1.0],
                    }
                }
            self.assertLessEqual(end, date(2026, 4, 15))
            self.assertEqual(start, date(2026, 3, 3))
            return {"daily": daily}

        def _loader(_lat, _lon, start, end):
            payload = _open(_lat, _lon, start, end, use_archive=True)
            return payload["daily"], "open-meteo", None

        result = fetch_period_weather(
            latitude=37.252,
            longitude=112.086,
            period_start="2026-04-02",
            period_end_inclusive="2026-04-15",
            daily_loader=_loader,
        )
        self.assertEqual(result["weather_source"], "open-meteo")
        self.assertIsNone(result["weather_error"])
        self.assertEqual(result["weather_period"]["precipitation_mm"], 14.0)
        self.assertEqual(result["weather_period"]["min_c"], 2.0)
        self.assertEqual(result["weather_period"]["max_c"], 12.0)
        self.assertEqual(result["weather_period"]["mean_c"], 7.0)
        self.assertEqual(result["weather_same_period_prior_year"]["mean_c"], 5.0)
        self.assertEqual(result["weather_antecedent"]["precipitation_mm"], 1.0)
        # Chart series includes the 30-day antecedent plus the period.
        days = [row["date"] for row in result["weather_daily"]]
        self.assertEqual(days[0], "2026-03-03")
        self.assertEqual(days[-1], "2026-04-15")
        self.assertIn("2026-04-02", days)

    def test_fetch_falls_back_to_nasa_and_does_not_invent_et0(self):
        from app.reports.biweekly.weather_fetch import fetch_period_weather

        nasa_daily = {
            "time": ["2026-04-02", "2026-04-03"],
            "precipitation_sum": [0.4, 0.6],
            "temperature_2m_min": [3.0, 4.0],
            "temperature_2m_max": [13.0, 14.0],
        }

        def _loader(_lat, _lon, start, end):
            if start.year == 2025:
                return None, None, "no_prior_year"
            return nasa_daily, "nasa-power", None

        result = fetch_period_weather(
            latitude=37.252,
            longitude=112.086,
            period_start="2026-04-02",
            period_end_inclusive="2026-04-03",
            antecedent_days=0,
            daily_loader=_loader,
        )
        self.assertEqual(result["weather_source"], "nasa-power")
        self.assertEqual(result["weather_period"]["precipitation_mm"], 1.0)
        self.assertIsNone(result["weather_period"]["et0_mm"])

    def test_centroid_from_products(self):
        from app.reports.biweekly.weather_fetch import centroid_from_products, resolve_report_lat_lon

        product = _synthetic_product("2026-04-09")
        lat, lon = centroid_from_products([product])
        self.assertAlmostEqual(lat, 37.252375, places=5)
        self.assertAlmostEqual(lon, 112.085, places=3)
        resolved = resolve_report_lat_lon(products=[product])
        self.assertEqual(resolved, (lat, lon))

    def test_blank_period_is_filled_from_forecast_without_replacing_older_days(self):
        from app.reports.biweekly.weather_fetch import _supplement_period_climate

        existing = [
            {
                "date": "2026-03-20",
                "precipitation_mm": 1.0,
                "min_c": 0.0,
                "max_c": 8.0,
                "et0_mm": 1.2,
            }
        ]

        def _forecast(_lat, _lon, start, end, *, use_archive=False):
            self.assertFalse(use_archive)
            self.assertEqual(start, date(2026, 4, 2))
            self.assertEqual(end, date(2026, 4, 15))
            return {
                "daily": {
                    "time": ["2026-04-02", "2026-04-03"],
                    "precipitation_sum": [3.0, 0.0],
                    "temperature_2m_min": [2.0, 3.0],
                    "temperature_2m_max": [12.0, 13.0],
                    "et0_fao_evapotranspiration": [1.5, 1.6],
                }
            }

        rows, source, error = _supplement_period_climate(
            37.25,
            112.08,
            "2026-04-02",
            "2026-04-15",
            existing,
            "open-meteo",
            None,
            forecast_fetcher=_forecast,
        )
        self.assertIsNone(error)
        self.assertEqual(source, "open-meteo")
        by_date = {row["date"]: row for row in rows}
        self.assertEqual(by_date["2026-03-20"]["precipitation_mm"], 1.0)
        self.assertEqual(by_date["2026-04-02"]["precipitation_mm"], 3.0)
        self.assertEqual(by_date["2026-04-03"]["min_c"], 3.0)

    def test_period_gap_uses_nasa_when_forecast_fails(self):
        from app.reports.biweekly.weather_fetch import _supplement_period_climate

        def _forecast(*_args, **_kwargs):
            raise RuntimeError("forecast down")

        def _nasa(_lat, _lon, start, end):
            self.assertEqual(start, date(2026, 4, 2))
            return {
                "daily": {
                    "time": ["2026-04-02"],
                    "precipitation_sum": [4.5],
                    "temperature_2m_min": [1.0],
                    "temperature_2m_max": [10.0],
                }
            }

        rows, source, error = _supplement_period_climate(
            37.25,
            112.08,
            "2026-04-02",
            "2026-04-15",
            [],
            "open-meteo",
            "no_data",
            forecast_fetcher=_forecast,
            nasa_fetcher=_nasa,
        )
        self.assertIsNone(error)
        self.assertEqual(source, "nasa-power")
        self.assertEqual(rows[0]["precipitation_mm"], 4.5)
        self.assertEqual(rows[0]["min_c"], 1.0)

    def test_apply_fills_nulls_and_keeps_a_measured_day(self):
        from app.reports.biweekly.weather_fetch import (
            apply_fetched_weather,
            period_has_usable_weather,
        )

        blank = {
            "weather_daily": [
                {
                    "date": "2026-04-02",
                    "precipitation_mm": None,
                    "min_c": None,
                    "max_c": None,
                }
            ],
            "weather_period": {"days": 1, "precipitation_mm": None, "min_c": None},
        }
        self.assertFalse(
            period_has_usable_weather(
                blank, start="2026-04-02", end_inclusive="2026-04-15"
            )
        )
        self.assertTrue(
            period_has_usable_weather(
                {"weather_period": {"precipitation_mm": 0.0}},
                start="2026-04-02",
                end_inclusive="2026-04-15",
            )
        )
        out = apply_fetched_weather(
            {
                "weather_daily": [
                    {
                        "date": "2026-04-02",
                        "precipitation_mm": 4.0,
                        "min_c": None,
                        "max_c": None,
                        "et0_mm": None,
                    },
                    {
                        "date": "2026-04-03",
                        "precipitation_mm": None,
                        "min_c": None,
                        "max_c": None,
                        "et0_mm": None,
                    },
                ]
            },
            {
                "weather_daily": [
                    {
                        "date": "2026-04-02",
                        "precipitation_mm": 9.0,
                        "min_c": 1.0,
                        "max_c": 12.0,
                        "et0_mm": 2.0,
                    },
                    {
                        "date": "2026-04-03",
                        "precipitation_mm": 0.0,
                        "min_c": 2.0,
                        "max_c": 13.0,
                        "et0_mm": 2.0,
                    },
                ],
                "weather_source": "open-meteo",
                "weather_same_period_prior_year": {
                    "mean_c": 6.0,
                    "precipitation_mm": 8.0,
                },
            },
            start="2026-04-02",
            end_inclusive="2026-04-15",
        )
        by_date = {row["date"]: row for row in out["weather_daily"]}
        self.assertEqual(by_date["2026-04-02"]["precipitation_mm"], 4.0)
        self.assertEqual(by_date["2026-04-02"]["min_c"], 1.0)
        self.assertEqual(by_date["2026-04-03"]["precipitation_mm"], 0.0)
        self.assertEqual(out["weather_period"]["precipitation_mm"], 4.0)
        self.assertEqual(out["weather_period"]["min_c"], 1.0)
        self.assertEqual(out["weather_source"], "open-meteo")
        self.assertEqual(out["weather_same_period_prior_year"]["precipitation_mm"], 8.0)
        self.assertIsNone(out["weather_error"])

    def test_apply_keeps_usable_weather_when_fetch_is_empty(self):
        from app.reports.biweekly.weather_fetch import apply_fetched_weather

        out = apply_fetched_weather(
            {
                "weather_daily": [
                    {
                        "date": "2026-04-02",
                        "precipitation_mm": 4.0,
                        "min_c": 2.0,
                        "max_c": 11.0,
                        "et0_mm": 1.0,
                    }
                ],
                "weather_source": "stored",
            },
            {
                "weather_daily": [],
                "weather_error": "open_meteo_down",
                "weather_source": None,
            },
            start="2026-04-02",
            end_inclusive="2026-04-15",
        )
        self.assertEqual(out["weather_period"]["precipitation_mm"], 4.0)
        self.assertEqual(out["weather_period"]["min_c"], 2.0)
        self.assertEqual(out["weather_source"], "stored")
        self.assertIsNone(out["weather_error"])


class ContextMapTests(unittest.TestCase):
    def test_infield_variation_from_synthetic_pixels(self):
        from app.reports.biweekly.context_maps import build_infield_variation

        products = [
            _synthetic_product("2026-03-27", offset=-0.02, stac_item_id="prior"),
            _synthetic_product("2026-04-09", offset=0.0),
        ]
        dest = Path(tempfile.mkdtemp()) / "zones.png"
        built = build_infield_variation(
            products=products,
            period={"start": "2026-04-02", "end_exclusive": "2026-04-16"},
            phase={"primary_metrics": ["ndre", "evi"]},
            dest=dest,
        )
        self.assertIsNotNone(built)
        self.assertTrue(Path(built["image"]).is_file())
        self.assertEqual(built["metric"], "ndre")
        self.assertEqual(built["date"], "2026-04-09")
        self.assertEqual(built["prior_date"], "2026-03-27")
        # 只有 NDRE 一列像元：EVI 不出图，也不拿别的指标顶替。
        self.assertEqual([item["metric"] for item in built["metrics"]], ["ndre"])
        first = built["metrics"][0]
        self.assertEqual(first["stats"]["status"], "ok")
        self.assertEqual(first["stats"]["n_valid"], 36)
        self.assertAlmostEqual(first["change"]["median"], 0.02, places=3)
        self.assertEqual(first["change"]["n_paired"], 36)

    def test_infield_skips_ineligible_cloudy_date(self):
        from app.reports.biweekly.context_maps import build_infield_variation

        cloudy = _synthetic_product("2026-04-14", offset=0.05)
        cloudy["parcel_cloud_cover_pct"] = None
        cloudy["cloud_cover"] = 39.555049
        products = [
            _synthetic_product("2026-03-27", offset=-0.02, stac_item_id="prior"),
            _synthetic_product("2026-04-09", offset=0.0),
            cloudy,
        ]
        dest = Path(tempfile.mkdtemp()) / "zones.png"
        built = build_infield_variation(
            products=products,
            period={"start": "2026-04-02", "end_exclusive": "2026-04-16"},
            phase={"primary_metrics": ["ndre", "evi"]},
            dest=dest,
        )
        self.assertIsNotNone(built)
        self.assertEqual(built["date"], "2026-04-09")
        self.assertNotEqual(built["date"], "2026-04-14")

    def test_surrounding_classifies_field_and_rings(self):
        from app.reports.biweekly.context_maps import build_surrounding_comparison

        # 10 km window around the 19994 processing bounds, synthetic RGB.
        height, width = 200, 200
        rgb = np.zeros((height, width), dtype=np.uint8)
        rgb = np.stack(
            [
                np.full((height, width), 70, dtype=np.uint8),
                np.full((height, width), 110, dtype=np.uint8),
                np.full((height, width), 50, dtype=np.uint8),
            ],
            axis=-1,
        )
        # Dark water strip at the top.
        rgb[:8, :] = (4, 6, 8)
        # Bright cloud strip at the bottom.
        rgb[-6:, :] = (230, 230, 230)
        bounds = (112.029821, 37.207627, 112.142607, 37.297732)
        product = _synthetic_product("2026-04-09")
        cols = product["pixel_data"]["columns"]
        dest = Path(tempfile.mkdtemp()) / "surround.png"
        built = build_surrounding_comparison(
            rgb=rgb,
            bounds=bounds,
            field_lons=np.asarray(cols["lon"]),
            field_lats=np.asarray(cols["lat"]),
            dest=dest,
            date="2026-04-09",
        )
        self.assertIsNotNone(built)
        self.assertTrue(Path(built["image"]).is_file())
        self.assertGreater(built["stats"]["field"]["n"], 0)
        self.assertGreater(built["stats"]["surrounding_1km"]["n"], 0)
        joined = "。".join(built["text"]) if isinstance(built["text"], list) else str(built["text"])
        self.assertIn("谁更绿", joined)
        self.assertNotIn("超绿指数", joined)
        self.assertNotIn("绿度", joined)
        self.assertIn("不是项目登记地块", joined)


class EnrichmentAndGenerateTests(unittest.TestCase):
    def test_enrich_attaches_weather_and_maps_without_db(self):
        from app.reports.biweekly.enrichment import enrich_official_report_facts

        facts = {
            "land_id": "19994",
            "land_name": "北侯1号地块",
            "period": {
                "start": "2026-04-02",
                "end_inclusive": "2026-04-15",
                "end_exclusive": "2026-04-16",
            },
            "phase": {"primary_metrics": ["ndre", "evi"], "plan_source": "native"},
        }
        products = [
            _synthetic_product("2026-03-27", offset=-0.02),
            _synthetic_product("2026-04-09"),
        ]
        height, width = 160, 160
        rgb = np.stack(
            [
                np.full((height, width), 70, dtype=np.uint8),
                np.full((height, width), 110, dtype=np.uint8),
                np.full((height, width), 50, dtype=np.uint8),
            ],
            axis=-1,
        )

        def _weather(**_kwargs):
            return {
                "weather_daily": [
                    {"date": "2026-04-02", "precipitation_mm": 1.0, "min_c": 4, "max_c": 16, "et0_mm": 2.0}
                ],
                "weather_period": {"days": 1, "precipitation_mm": 1.0, "et0_mm": 2.0, "min_c": 4, "max_c": 16},
                "weather_antecedent": {"days": 0, "precipitation_mm": 0.0, "et0_mm": None},
                "weather_source": "open-meteo",
                "weather_error": None,
            }

        figures = Path(tempfile.mkdtemp())
        filled = enrich_official_report_facts(
            facts,
            figures_dir=figures,
            products=products,
            fetch_weather=_weather,
            fetch_truecolor=lambda **_k: rgb,
        )
        self.assertEqual(filled["weather_period"]["precipitation_mm"], 1.0)
        self.assertEqual(filled["weather_source"], "open-meteo")
        self.assertTrue(Path(filled["spatial_variation_image"]).is_file())
        self.assertTrue(Path(filled["surrounding_comparison_image"]).is_file())
        self.assertEqual([item["metric"] for item in filled["infield"]], ["ndre"])
        self.assertEqual(filled["report_metrics"]["primary"], ["ndre", "evi"])
        self.assertEqual(filled["comparison"]["latest_date"], "2026-04-09")
        self.assertEqual(filled["comparison"]["baseline_date"], "2026-03-27")
        self.assertEqual(filled["comparison"]["metrics"]["ndre"]["direction"], "up")
        # 产品里没有 EVI 列：保持缺测，不写成 0。
        self.assertIsNone(filled["comparison"]["metrics"]["evi"]["latest"])
        surround = filled["surrounding_comparison"]
        surround_txt = "。".join(surround) if isinstance(surround, list) else str(surround)
        self.assertIn("项目登记地块", surround_txt)

    def test_enrich_searches_stac_when_product_lacks_item_id(self):
        from app.reports.biweekly.enrichment import enrich_official_report_facts

        product = _synthetic_product("2026-04-09")
        product.pop("stac_item_id", None)
        product.pop("processing_window", None)
        product["scene_id"] = "stac_bridge_2026-04-09_S2"
        height, width = 160, 160
        rgb = np.stack(
            [
                np.full((height, width), 70, dtype=np.uint8),
                np.full((height, width), 110, dtype=np.uint8),
                np.full((height, width), 50, dtype=np.uint8),
            ],
            axis=-1,
        )
        searched = {}

        def _search(bounds, start, end):
            searched["bounds"] = bounds
            searched["start"] = start.isoformat()
            searched["end"] = end.isoformat()
            return [{"id": "S2C_49SEB_20260409_0_L2A", "properties": {"datetime": "2026-04-09T03:27:38Z", "eo:cloud_cover": 14.3}}]

        def _truecolor(*, stac_item_id, bounds, **_k):
            searched["item"] = stac_item_id
            searched["used_bounds"] = bounds
            return rgb

        with patch("app.tasks.project_cogs.search_items", side_effect=_search):
            filled = enrich_official_report_facts(
                {
                    "land_id": "19994",
                    "period": {"start": "2026-04-02", "end_inclusive": "2026-04-15", "end_exclusive": "2026-04-16"},
                    "phase": {"primary_metrics": ["ndre"]},
                    "optical_days": ["2026-04-09"],
                    "weather_daily": [{"date": "2026-04-02", "precipitation_mm": 0, "min_c": 1, "max_c": 2, "et0_mm": 1}],
                    "weather_period": {"precipitation_mm": 0, "et0_mm": 1},
                },
                figures_dir=Path(tempfile.mkdtemp()),
                products=[product],
                fetch_truecolor=_truecolor,
            )
        self.assertEqual(searched.get("item"), "S2C_49SEB_20260409_0_L2A")
        self.assertEqual(searched.get("start"), "2026-04-09")
        self.assertTrue(filled.get("surrounding_comparison_image"))
        self.assertGreater(searched["bounds"][2] - searched["bounds"][0], 0.05)

    def test_weather_failure_is_marked_missing_not_invented(self):
        from app.reports.biweekly.enrichment import enrich_official_report_facts

        facts = {
            "land_id": "19994",
            "period": {"start": "2026-04-02", "end_inclusive": "2026-04-15", "end_exclusive": "2026-04-16"},
            "phase": {"primary_metrics": ["ndre"]},
        }

        def _weather(**_kwargs):
            return {
                "weather_daily": [],
                "weather_period": {"days": 0, "precipitation_mm": None, "et0_mm": None},
                "weather_antecedent": {"days": 0, "precipitation_mm": None, "et0_mm": None},
                "weather_source": None,
                "weather_error": "open_meteo_down",
            }

        filled = enrich_official_report_facts(
            facts,
            figures_dir=Path(tempfile.mkdtemp()),
            products=[],
            latitude=37.25,
            longitude=112.08,
            fetch_weather=_weather,
            fetch_truecolor=lambda **_k: None,
        )
        self.assertEqual(filled["weather_error"], "open_meteo_down")
        self.assertFalse(filled.get("weather_daily"))
        self.assertIsNone((filled.get("weather_period") or {}).get("precipitation_mm"))
        self.assertNotIn("surrounding_comparison", filled)
        self.assertTrue(filled["surrounding_missing_reason"])
        self.assertEqual(filled["infield"], [])
        self.assertNotIn("spatial_variation_image", filled)

    def test_blank_stored_weather_is_compensated_before_the_report(self):
        from app.reports.biweekly.enrichment import enrich_official_report_facts

        calls = {}

        def _weather(**kwargs):
            calls["start"] = kwargs["period_start"]
            calls["end"] = kwargs["period_end_inclusive"]
            return {
                "weather_daily": [
                    {
                        "date": "2026-03-20",
                        "precipitation_mm": 2.0,
                        "min_c": 1.0,
                        "max_c": 9.0,
                        "et0_mm": 1.5,
                    },
                    {
                        "date": "2026-04-02",
                        "precipitation_mm": 6.5,
                        "min_c": 3.0,
                        "max_c": 14.0,
                        "et0_mm": 2.0,
                    },
                ],
                "weather_source": "open-meteo",
                "weather_same_period_prior_year": {
                    "mean_c": 5.0,
                    "precipitation_mm": 11.0,
                },
                "weather_error": None,
            }

        filled = enrich_official_report_facts(
            {
                "land_id": "19994",
                "period": {
                    "start": "2026-04-02",
                    "end_inclusive": "2026-04-15",
                    "end_exclusive": "2026-04-16",
                },
                "phase": {"primary_metrics": ["ndre"]},
                "weather_daily": [
                    {
                        "date": "2026-03-20",
                        "precipitation_mm": None,
                        "min_c": None,
                        "max_c": None,
                    },
                    {
                        "date": "2026-04-02",
                        "precipitation_mm": None,
                        "min_c": None,
                        "max_c": None,
                    },
                ],
                "weather_period": {
                    "days": 1,
                    "precipitation_mm": None,
                    "min_c": None,
                    "max_c": None,
                },
            },
            figures_dir=Path(tempfile.mkdtemp()),
            products=[],
            latitude=37.25,
            longitude=112.08,
            fetch_weather=_weather,
            fetch_truecolor=lambda **_k: None,
        )
        self.assertEqual(calls["start"], "2026-04-02")
        self.assertEqual(calls["end"], "2026-04-15")
        self.assertEqual(filled["weather_period"]["precipitation_mm"], 6.5)
        self.assertEqual(filled["weather_period"]["min_c"], 3.0)
        self.assertEqual(filled["weather_period"]["max_c"], 14.0)
        self.assertEqual(filled["weather_antecedent"]["precipitation_mm"], 2.0)
        self.assertEqual(
            filled["weather_same_period_prior_year"]["precipitation_mm"], 11.0
        )
        self.assertEqual(filled["weather_source"], "open-meteo")
        self.assertIsNone(filled["weather_error"])

    def test_generate_entry_renders_pdf_with_context_images(self):
        from app.reports.biweekly.generate import generate_official_biweekly_pdf
        from app.reports.biweekly.infield_maps import build_infield_metric

        env = {key: value for key, value in os.environ.items() if not key.startswith("BIWEEKLY_FONT")}
        dest = Path(tempfile.mkdtemp()) / "official.pdf"
        figure = Path(tempfile.mkdtemp())
        surround = _tiny_png(figure / "surrounding_farmland.png")
        facts = {
            "land_id": "19994",
            "land_name": "北侯1号地块",
            "policy_version": "biweekly-wheat-display-v8-1",
            "period": {
                "period_id": "14",
                "start": "2026-04-02",
                "end_inclusive": "2026-04-15",
                "end_exclusive": "2026-04-16",
                "s1_products": 1,
            },
            "phase": {
                "display_phase": "S2",
                "primary_metrics": ["ndre", "evi"],
                "supporting_metrics": ["ndvi", "ndmi"],
                "plan_source": "native",
                "plan_label": "本田方案",
            },
            "cutoff": "2026-04-16T00:00:00+08:00",
            "optical_days": ["2026-04-04", "2026-04-09"],
            "observations": [
                {
                    "date": "2026-04-09",
                    "sensor": "S2",
                    "ndvi_avg": 0.11,
                    "evi_avg": 0.08,
                    "ndre_avg": 0.05,
                    "ndmi_avg": -0.02,
                }
            ],
            "s1_count": 1,
            "in_period_radar": True,
        }

        def _enrich(payload, **_kwargs):
            filled = dict(payload)
            filled["weather_daily"] = [
                {"date": "2026-04-02", "precipitation_mm": 0.5, "min_c": 4, "max_c": 16, "et0_mm": 2.2}
            ]
            filled["weather_period"] = {"precipitation_mm": 0.5, "et0_mm": 2.2, "min_c": 4, "max_c": 16}
            filled["infield"] = [
                build_infield_metric(
                    metric="ndre",
                    latest=_synthetic_product("2026-04-09"),
                    baseline=_synthetic_product("2026-04-04", offset=-0.02),
                    dest=figure / "infield_ndre.png",
                )
            ]
            filled["surrounding_comparison"] = "本田与周边 1 公里影像农田对照见图。"
            filled["surrounding_comparison_image"] = str(surround)
            return filled

        with patch.dict(os.environ, env, clear=True), patch(
            "app.reports.biweekly.generate.enrich_official_report_facts",
            side_effect=_enrich,
        ):
            path = generate_official_biweekly_pdf(facts, dest, figures_dir=figure)
        self.assertTrue(path.is_file())
        data = path.read_bytes()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(path.stat().st_size, 1000)
        # Both context figures should be embedded as XObjects.
        self.assertGreaterEqual(data.count(b"/XObject"), 1)
        self.assertIn(b"/Image", data)



class CoverAssetTests(unittest.TestCase):
    def test_bundled_cover_assets_exist_without_env(self):
        from app.reports.biweekly.cover import BACKGROUND_NAME, LOGO_NAME, bundled_asset_dir, resolve_cover_assets

        directory = bundled_asset_dir()
        self.assertTrue((directory / BACKGROUND_NAME).is_file())
        self.assertTrue((directory / LOGO_NAME).is_file())
        background, logo = resolve_cover_assets()
        self.assertEqual(background.parent, directory)
        self.assertIsNotNone(logo)

    def test_compose_official_cover_uses_bundled_background(self):
        from app.reports.biweekly.cover import compose_official_cover, compact_date_span

        facts = {
            "land_name": "北侯1号地块",
            "crop_name": "小麦",
            "period": {"start": "2026-04-02", "end_inclusive": "2026-04-15", "end_exclusive": "2026-04-16"},
            "phase": {
                "display_phase": "S2",
                "phase_name": "营养/生殖并进",
                "plan_source": "native",
                "plan_label": "本田方案",
                "observation_stages": [{"stage_code": "拔节孕穗期"}],
            },
            "report_date": "2026-11-22",
        }
        dest = Path(tempfile.mkdtemp()) / "cover.png"
        path = compose_official_cover(facts, dest)
        self.assertTrue(path.is_file())
        self.assertGreater(path.stat().st_size, 20000)
        self.assertEqual(compact_date_span(facts["period"]), "2026年4月2日—15日")

    def test_conclusion_states_direction_without_claiming_improvement(self):
        from app.reports.biweekly.enrichment import ensure_report_inputs
        from app.reports.biweekly.official_pdf import render_official_biweekly_pdf
        from app.reports.biweekly.report_text import evidence_table

        facts = {
            "land_id": "19994",
            "land_name": "北侯1号地块",
            "crop_name": "小麦",
            "period": {"start": "2026-04-02", "end_inclusive": "2026-04-15", "end_exclusive": "2026-04-16", "s1_products": 1},
            "phase": {
                "display_phase": "S2",
                "phase_name": "营养/生殖并进",
                "primary_metrics": ["ndre", "evi"],
                "supporting_metrics": ["ndvi", "ndmi"],
                "plan_source": "native",
                "plan_label": "本田方案",
                "observation_stages": [{"stage_code": "拔节孕穗期", "phase_label": "S2"}],
            },
            "observations": [
                {"date": "2026-04-04", "ndvi_avg": 0.15, "evi_avg": 0.08, "ndre_avg": 0.056, "ndmi_avg": 0.02},
                {"date": "2026-04-09", "ndvi_avg": 0.11, "evi_avg": 0.086, "ndre_avg": 0.048, "ndmi_avg": -0.02},
            ],
            "prior_observations": [
                {"date": "2026-03-20", "ndvi_avg": 0.17, "evi_avg": 0.08, "ndre_avg": 0.018, "ndmi_avg": 0.19},
                {"date": "2026-03-27", "ndvi_avg": 0.11, "evi_avg": 0.066, "ndre_avg": 0.006, "ndmi_avg": 0.10},
            ],
            "weather_period": {"precipitation_mm": 3.3, "et0_mm": 64.7, "days": 14},
            "in_period_radar": True,
            "s1_count": 1,
        }
        filled = ensure_report_inputs(dict(facts))
        # 文字、表格与图用同一对日期：本期最近一次观测对比它之前的那一次。
        self.assertEqual(filled["comparison"]["latest_date"], "2026-04-09")
        self.assertEqual(filled["comparison"]["baseline_date"], "2026-04-04")
        self.assertEqual(filled["comparison"]["metrics"]["ndre"]["direction"], "down")
        self.assertEqual(filled["comparison"]["metrics"]["evi"]["direction"], "up")
        table = evidence_table(filled)
        self.assertEqual(table[0][0], "指标")
        self.assertEqual([row[0] for row in table[1:]], ["NDRE（主要）", "EVI（主要）", "NDVI（辅助）", "NDMI（辅助）"])
        dest = Path(tempfile.mkdtemp()) / "out.pdf"
        path = render_official_biweekly_pdf(facts, dest)
        self.assertTrue(path.read_bytes().startswith(b"%PDF"))
        self.assertGreater(path.stat().st_size, 20000)


if __name__ == "__main__":
    unittest.main()
