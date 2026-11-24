"""Offline rendering of official biweekly PDFs from exported files.

测试输入由样稿生成器按真实导出文件的形状现场生成（测试地块 T900，去掉示例标记），
只用于检验流程；不是任何真实地块的数据，全程不联网。
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "biweekly_offline_render.py"
LAND = "T900"


def _real_shaped_products() -> list[dict]:
    """Products laid out like the OSS scene JSON: scene columns on top, STAC id inside pixel_data."""
    from app.reports.biweekly import demo_data

    grid = demo_data.field_grid()
    products = []
    for case in demo_data.CASES.values():
        for scene in case["scenes"]:
            product = demo_data.build_product(case, scene, grid)
            product.pop("data_kind")
            product.pop("data_notice")
            product["land_id"] = LAND
            product["land_name"] = "测试地块"
            product["scene_id"] = f"stac_bridge_{product['date']}_S2"
            stac_id = product.pop("stac_item_id")
            window = product.pop("processing_window")
            if "pixel_data" in product:
                product["pixel_data"]["stac_item_id"] = stac_id
                product["pixel_data"]["processing_window"] = window
            products.append(product)
    return products


def _smart_plan_rows() -> dict:
    from app.reports.biweekly import demo_data

    tasks = []
    for index, item in enumerate(demo_data.PLAN_INTERVALS, start=1):
        finish = date.fromisoformat(item["end_exclusive"]) - timedelta(days=1)
        tasks.append(
            {
                "plant_task_id": index,
                "plan_id": 77,
                "task_name": item["stage_code"],
                "plan_start": item["start"],
                "plan_finish": finish.isoformat(),
                "del_flag": "0",
            }
        )
    batch = {"batch_id": 77, "land_id": LAND, "crop_id": 4, "crop_name": "小麦", "batch_name": "测试批次"}
    return {"batches": [batch], "tasks": tasks}


def _write_inputs(root: Path) -> dict[str, Path]:
    from app.reports.biweekly import demo_data

    products_dir = root / "products"
    products_dir.mkdir(parents=True)
    for product in _real_shaped_products():
        (products_dir / f"{product['date']}_S2.json").write_text(json.dumps(product), encoding="utf-8")
    plan = root / "plan.json"
    plan.write_text(json.dumps(_smart_plan_rows(), ensure_ascii=False), encoding="utf-8")
    weather = root / "weather.csv"
    with weather.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["land_id", "date", "temperature_2m_min", "temperature_2m_max", "precipitation_sum", "et0_fao_mm"]
        )
        for row in demo_data.build_weather(demo_data.CASES["s3-grainfill"]):
            writer.writerow(
                [
                    LAND,
                    row["date"],
                    row["min_c"],
                    row["max_c"],
                    "" if row["precipitation_mm"] is None else row["precipitation_mm"],
                    "" if row["et0_mm"] is None else row["et0_mm"],
                ]
            )
    return {"products": products_dir, "plan": plan, "weather": weather}


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "".join((page.extract_text() or "") for page in reader.pages).replace("\n", "").replace(" ", "")


class PlanCalendarTests(unittest.TestCase):
    def test_smart_rows_give_the_official_season_and_periods(self) -> None:
        from agric_satellite_analysis_common.biweekly.calendar import iter_periods
        from app.reports.biweekly.offline_render import calendar_from_plan, export_windows

        calendar = calendar_from_plan(LAND, _smart_plan_rows())
        self.assertTrue(calendar["plan_available"])
        season = calendar["seasons"][0]
        self.assertEqual(season["season_id"], "2025/26")
        self.assertEqual((season["start"], season["end_inclusive"]), ("2025-10-05", "2026-06-09"))
        self.assertEqual(season["observation_start"], "2025-10-12")
        official = iter_periods("2025-10-12", "2026-06-09")
        self.assertEqual(
            [(row["period_id"], row["start"], row["end_exclusive"]) for row in season["periods"]],
            [(row["period_id"], row["start"], row["end_exclusive"]) for row in official],
        )
        self.assertTrue(season["periods"][-1]["partial"])
        self.assertEqual(season["native_plan"]["phenology_basis"], "PLAN_DERIVED_FROM_SMART_PLANT_TASK")

        ranges = export_windows(calendar)[0]["export_ranges"]
        self.assertEqual(ranges["scenes"]["date_from"], "2025-10-05")
        self.assertEqual(ranges["products"]["date_from"], "2025-09-07")
        self.assertEqual(ranges["weather"]["date_from"], "2025-09-05")

    def test_api_matches_the_shared_builder(self) -> None:
        """接口机用的就是公共包里的同一个函数，离线与线上不会各算各的。"""
        from agric_satellite_analysis_common.biweekly import plan_calendar

        api_source = REPO_ROOT / "services" / "api" / "app" / "services" / "biweekly_plans.py"
        if not api_source.is_file():
            self.skipTest("接口机代码不在本目录（离线代码包只带出报所需的代码）")
        api_module = api_source.read_text(encoding="utf-8")
        self.assertIn("agric_satellite_analysis_common.biweekly.plan_calendar", api_module)
        self.assertNotIn("def build_seasons_from_rows", api_module)
        self.assertTrue(callable(plan_calendar.build_seasons_from_rows))

    def test_api_seasons_payload_is_accepted_and_checked(self) -> None:
        from app.reports.biweekly.offline_render import OfflineInputError, calendar_from_plan

        built = calendar_from_plan(LAND, _smart_plan_rows())
        api_payload = {"land_id": LAND, "seasons": json.loads(json.dumps(built["seasons"]))}
        again = calendar_from_plan(LAND, api_payload)
        self.assertEqual(again["plan_input"], "api_seasons")
        self.assertEqual(
            [row["start"] for row in again["seasons"][0]["periods"]],
            [row["start"] for row in built["seasons"][0]["periods"]],
        )

        one_season = {**api_payload["seasons"][0], "land_id": LAND}
        self.assertEqual(calendar_from_plan(LAND, one_season)["plan_input"], "api_periods")

        tampered = json.loads(json.dumps(api_payload))
        tampered["seasons"][0]["periods"][3]["start"] = "2025-11-17"
        with self.assertRaises(OfflineInputError):
            calendar_from_plan(LAND, tampered)

    def test_historical_full_season_export_keeps_its_periods(self) -> None:
        from agric_satellite_analysis_common.biweekly.calendar import iter_periods
        from app.reports.biweekly.offline_render import calendar_from_plan

        built = calendar_from_plan(LAND, _smart_plan_rows())
        historical = {"land_id": LAND, "seasons": json.loads(json.dumps(built["seasons"]))}
        legacy = iter_periods("2025-10-05", "2026-06-09")
        historical["seasons"][0]["periods"] = [
            {
                "period_id": row["period_id"],
                "start": row["start"],
                "end_exclusive": row["end_exclusive"],
            }
            for row in legacy
        ]
        again = calendar_from_plan(LAND, historical)
        self.assertEqual(
            [row["start"] for row in again["seasons"][0]["periods"]],
            [row["start"] for row in legacy],
        )
        self.assertEqual(again["seasons"][0]["periods"][0]["start"], "2025-10-05")

    def test_plan_of_another_land_is_never_used(self) -> None:
        from app.reports.biweekly.offline_render import OfflineInputError, calendar_from_plan

        rows = _smart_plan_rows()
        other = calendar_from_plan("T901", rows)
        self.assertFalse(other["plan_available"])
        self.assertEqual(other["seasons"], [])

        api_payload = {"land_id": LAND, "seasons": calendar_from_plan(LAND, rows)["seasons"]}
        with self.assertRaises(OfflineInputError):
            calendar_from_plan("T901", api_payload)

    def test_deleted_plan_rows_are_dropped_like_the_loader_sql(self) -> None:
        from app.reports.biweekly.offline_render import calendar_from_plan

        rows = _smart_plan_rows()
        rows["tasks"].append(
            {
                "plant_task_id": 99,
                "plan_id": 77,
                "task_name": "成熟期",
                "plan_start": "2026-06-10",
                "plan_finish": "2026-08-30",
                "del_flag": "2",
            }
        )
        season = calendar_from_plan(LAND, rows)["seasons"][0]
        self.assertEqual(season["end_inclusive"], "2026-06-09")


class InputRowTests(unittest.TestCase):
    def test_scene_rows_follow_the_official_eligibility_rule(self) -> None:
        from app.reports.biweekly.offline_render import observations_between, scene_rows_from_products

        rows = scene_rows_from_products(_real_shaped_products())
        found = {
            row["date"]: row
            for row in observations_between(rows, "2025-11-02", "2025-11-16", source="parcel_product_json")
        }
        self.assertEqual(sorted(found), ["2025-11-03", "2025-11-07"])
        self.assertEqual(found["2025-11-03"]["quality_status"], "excluded")
        self.assertEqual(found["2025-11-03"]["exclusion_reason"], "cloud_over_limit")
        self.assertIsNone(found["2025-11-03"]["ndvi_avg"])
        self.assertEqual(found["2025-11-07"]["quality_status"], "eligible_descriptive")
        self.assertEqual(found["2025-11-07"]["source"], "parcel_product_json")

    def test_prior_observations_walk_back_to_the_last_period_with_optics(self) -> None:
        from app.reports.biweekly.offline_render import (
            calendar_from_plan,
            last_optical_observations,
            scene_rows_from_products,
        )

        periods = calendar_from_plan(LAND, _smart_plan_rows())["seasons"][0]["periods"]
        rows = scene_rows_from_products(_real_shaped_products())
        # 第13期（3月22日起）之前，第4至12期都没有观测；应一直回溯到第3期。
        prior = last_optical_observations(rows, periods, "13", source="x")
        self.assertEqual(sorted({row["date"] for row in prior}), ["2025-11-03", "2025-11-07"])
        self.assertEqual(last_optical_observations(rows, periods, "1", source="x"), [])

    def test_csv_blanks_stay_missing(self) -> None:
        from app.reports.biweekly.offline_render import load_rows, load_weather

        with tempfile.TemporaryDirectory() as tmp:
            scenes = Path(tmp) / "scenes.csv"
            scenes.write_text(
                "land_id,date,sensor,scene_id,ndvi_avg,evi_avg,ndre_avg,ndmi_avg,cloud_cover,parcel_cloud_cover_pct\n"
                f"{LAND},2026-04-09,S2,a,0.71,,NULL,\\N,12.5,\n",
                encoding="utf-8",
            )
            row = load_rows(scenes)[0]
            self.assertEqual(row["ndvi_avg"], "0.71")
            for key in ("evi_avg", "ndre_avg", "ndmi_avg", "parcel_cloud_cover_pct"):
                self.assertIsNone(row[key], msg=key)

            weather = Path(tmp) / "weather.csv"
            weather.write_text(
                "date,temperature_2m_min,temperature_2m_max,precipitation_sum,et0_fao_mm\n"
                "2026-04-09,8.5,21.0,,3.2\n",
                encoding="utf-8",
            )
            day = load_weather(weather, LAND)[0]
            self.assertEqual(day, {"date": "2026-04-09", "min_c": 8.5, "max_c": 21.0, "precipitation_mm": None, "et0_mm": 3.2})

    def test_inputs_of_another_land_are_rejected(self) -> None:
        from app.reports.biweekly.offline_render import (
            OfflineInputError,
            check_scene_rows,
            load_products,
            load_weather,
        )

        with tempfile.TemporaryDirectory() as tmp:
            paths = _write_inputs(Path(tmp))
            with self.assertRaises(OfflineInputError):
                load_products(paths["products"], "T901")
            with self.assertRaises(OfflineInputError):
                load_weather(paths["weather"], "T901")
        with self.assertRaises(OfflineInputError):
            check_scene_rows([{"land_id": "T901", "date": "2026-04-09", "sensor": "S2"}], LAND)

    def test_demo_products_cannot_be_rendered_as_a_real_land(self) -> None:
        from app.reports.biweekly import demo_data
        from app.reports.biweekly.offline_render import OfflineInputError, load_products

        case = demo_data.CASES["s3-grainfill"]
        product = demo_data.build_product(case, case["scenes"][0], demo_data.field_grid())
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / f"{product['date']}_S2.json").write_text(json.dumps(product), encoding="utf-8")
            with self.assertRaises(OfflineInputError) as caught:
                load_products(tmp, demo_data.LAND_ID)
        self.assertIn("示例数据", str(caught.exception))

    def test_product_file_rules(self) -> None:
        from app.reports.biweekly.offline_render import OfflineInputError, load_products

        base = _real_shaped_products()[0]
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / f"{base['date']}_S2.json").write_text(json.dumps(base), encoding="utf-8")
            (folder / f"{base['date']}_S2_decloud.json").write_text(json.dumps(base), encoding="utf-8")
            (folder / "export-copy.json").write_text(json.dumps(base), encoding="utf-8")
            products, notes = load_products(folder, LAND)
            self.assertEqual(len(products), 1)
            self.assertEqual(len(notes), 2)
            (folder / "export-copy-2.json").write_text(json.dumps(base), encoding="utf-8")
            (folder / f"{base['date']}_S2.json").unlink()
            with self.assertRaises(OfflineInputError):
                load_products(folder, LAND)

    def test_legacy_and_string_pixel_payloads_are_read_without_changing_values(self) -> None:
        from agric_satellite_analysis_common.lonlat_pixels import columns_to_pixels
        from app.reports.biweekly.enrichment import normalize_product
        from app.reports.biweekly.metric_stats import field_stats

        columnar = next(item for item in _real_shaped_products() if item.get("pixel_data"))
        expected = field_stats(columnar, "ndvi")
        self.assertEqual(expected["status"], "ok")

        legacy = {key: value for key, value in columnar.items() if key != "pixel_data"}
        legacy["pixel_data"] = {
            "format": "lonlat_v1",
            "pixels": columns_to_pixels(columnar["pixel_data"]["columns"]),
            "stac_item_id": columnar["pixel_data"]["stac_item_id"],
        }
        before = json.dumps(legacy, sort_keys=True)
        converted = normalize_product(legacy)
        self.assertEqual(json.dumps(legacy, sort_keys=True), before, "输入对象不应被修改")
        self.assertEqual(converted["pixel_data"]["format"], "lonlat_columns_v1")
        self.assertEqual(field_stats(converted, "ndvi"), expected)

        as_text = {**columnar, "pixel_data": json.dumps(columnar["pixel_data"])}
        self.assertEqual(field_stats(normalize_product(as_text), "ndvi"), expected)


class OfflineEntryTests(unittest.TestCase):
    def test_offline_entry_never_calls_a_fetcher_or_storage(self) -> None:
        from app.reports.biweekly.enrichment import enrich_official_report_facts
        from app.reports.biweekly.offline_render import build_period_facts, calendar_from_plan, scene_rows_from_products

        class Forbidden:
            def __getattr__(self, name):
                raise AssertionError(f"offline run touched storage.{name}")

        def forbidden(**_kwargs):
            raise AssertionError("offline run called a network fetcher")

        products = _real_shaped_products()
        facts, status = build_period_facts(
            land_id=LAND,
            calendar=calendar_from_plan(LAND, _smart_plan_rows()),
            season_id="2025/26",
            period_id="14",
            scene_rows=scene_rows_from_products(products),
            observation_source="parcel_product_json",
        )
        self.assertEqual(status, "ready")
        with tempfile.TemporaryDirectory() as tmp:
            # 不给产品也不回退到对象存储。
            bare = enrich_official_report_facts(
                facts,
                figures_dir=Path(tmp) / "a",
                storage=Forbidden(),
                fetch_weather=forbidden,
                fetch_truecolor=forbidden,
                offline=True,
            )
            self.assertEqual(bare["infield"], [])
            self.assertEqual(bare["surrounding_error"], "offline")
            self.assertIn("离线", bare["surrounding_missing_reason"])
            self.assertFalse(bare.get("weather_daily"))

            filled = enrich_official_report_facts(
                facts,
                figures_dir=Path(tmp) / "b",
                products=products,
                storage=Forbidden(),
                fetch_weather=forbidden,
                fetch_truecolor=forbidden,
                offline=True,
            )
            self.assertTrue(filled["infield"])
            self.assertNotIn("surrounding_comparison_image", filled)

    def test_facts_match_the_api_payload_shape_and_are_not_marked_demo(self) -> None:
        from app.reports.biweekly.offline_render import (
            build_period_facts,
            calendar_from_plan,
            load_weather,
            scene_rows_from_products,
        )

        with tempfile.TemporaryDirectory() as tmp:
            paths = _write_inputs(Path(tmp))
            weather = load_weather(paths["weather"], LAND)
        facts, status = build_period_facts(
            land_id=LAND,
            calendar=calendar_from_plan(LAND, _smart_plan_rows()),
            season_id="2025/26",
            period_id="16",
            scene_rows=scene_rows_from_products(_real_shaped_products()),
            observation_source="parcel_product_json",
            weather=weather,
            land_name="测试地块",
        )
        self.assertEqual(status, "ready")
        self.assertNotIn("data_kind", facts)
        self.assertNotIn("data_notice", facts)
        self.assertEqual(facts["land_id"], LAND)
        self.assertEqual(facts["period"]["start"], "2026-05-03")
        self.assertEqual(facts["optical_days"], ["2026-05-06", "2026-05-14"])
        self.assertEqual(facts["phase"]["plan_source"], "native")
        self.assertEqual(facts["phase"]["display_phase"], "S3")
        self.assertEqual(facts["observation_source"], "parcel_product_json")
        # 气象：本期行在前期行之后，前期最多取 30 天，合计只算有记录的天。
        days = [row["date"] for row in facts["weather_daily"]]
        self.assertEqual(days, sorted(days))
        self.assertGreaterEqual(min(days), "2026-04-11")
        self.assertEqual(facts["weather_period"]["start"], "2026-05-03")
        self.assertEqual(facts["weather_period"]["days"], 14)
        # 5月15、16日降水缺测：不计入合计，也不当作 0。
        self.assertEqual(facts["weather_period"]["precipitation_days"], 12)
        self.assertEqual(facts["weather_antecedent"]["end"], "2026-05-02")

    def test_product_selection_keeps_to_the_period_and_its_baseline(self) -> None:
        from app.reports.biweekly.offline_render import (
            build_period_facts,
            calendar_from_plan,
            products_for_period,
            scene_rows_from_products,
        )

        products = _real_shaped_products()
        facts, _ = build_period_facts(
            land_id=LAND,
            calendar=calendar_from_plan(LAND, _smart_plan_rows()),
            season_id="2025/26",
            period_id="13",
            scene_rows=scene_rows_from_products(products),
            observation_source="parcel_product_json",
        )
        used = sorted(item["date"] for item in products_for_period(products, facts))
        # 本期及回看 28 天内的产品，加上 facts 列为对比基准的 11 月观测；不含更早或之后的日期。
        self.assertEqual(used, ["2025-11-07", "2026-03-25", "2026-04-01", "2026-04-04"])


class RenderLandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.reports.biweekly.offline_render import (
            calendar_from_plan,
            load_products,
            load_weather,
            render_land,
            scene_rows_from_products,
        )

        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        paths = _write_inputs(root / "in")
        products, _ = load_products(paths["products"], LAND)
        cls.out = root / "out"
        cls.report = render_land(
            land_id=LAND,
            calendar=calendar_from_plan(LAND, _smart_plan_rows()),
            scene_rows=scene_rows_from_products(products),
            observation_source="parcel_product_json",
            products=products,
            out_dir=cls.out,
            weather=load_weather(paths["weather"], LAND),
            land_name="测试地块",
            period_ids=["1", "3", "16", "17", "18"],
            as_of="2026-05-20",
        )
        cls.periods = {row["period_id"]: row for row in cls.report["seasons"][0]["periods"]}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_period_without_optics_gets_a_brief_and_no_pdf(self) -> None:
        row = self.periods["1"]
        self.assertEqual(row["status"], "evidence_limited")
        folder = self.out / row["dir"]
        self.assertEqual(row["artifact"], "evidence-limited.json")
        self.assertEqual(list(folder.glob("*.pdf")), [])
        brief = json.loads((folder / "evidence-limited.json").read_text(encoding="utf-8"))
        self.assertEqual(brief["observations"], [])
        self.assertIn("没有可用光学观测", brief["brief"])

    def test_periods_not_yet_over_are_not_rendered(self) -> None:
        self.assertEqual(self.periods["17"]["status"], "in_progress")
        self.assertEqual(self.periods["18"]["status"], "not_started")
        for key in ("17", "18"):
            self.assertNotIn("dir", self.periods[key])

    def test_ready_period_is_an_official_pdf_with_real_labels(self) -> None:
        row = self.periods["16"]
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["artifact"], f"种植双周报_{LAND}_2026-05-03_2026-05-16.pdf")
        pdf = self.out / row["dir"] / row["artifact"]
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        text = _pdf_text(pdf)
        self.assertIn("测试地块", text)
        for banned in ("示例数据", "非实测", "样稿"):
            self.assertNotIn(banned, text)
        self.assertIn("本次为离线生成", text)
        self.assertEqual(row["phase"], "S3")
        self.assertEqual(row["primary_metrics"], ["ndre", "ndmi"])
        self.assertEqual(row["pixel_dates"], ["2026-05-06", "2026-05-14"])
        enriched = json.loads((self.out / row["dir"] / "facts-enriched.json").read_text(encoding="utf-8"))
        self.assertNotIn("data_kind", enriched)
        self.assertTrue(enriched["weather_daily"])

    def test_missing_weather_is_stated_not_invented(self) -> None:
        row = self.periods["3"]
        self.assertEqual(row["status"], "ready")
        self.assertEqual(row["rejected_optical"], 1)
        self.assertTrue(any(item.startswith("气象条件") for item in row["not_provided"]))
        self.assertTrue(any(item.startswith("和周围的农田比") for item in row["not_provided"]))
        text = _pdf_text(self.out / row["dir"] / row["artifact"])
        self.assertIn("未取得本期气温和降水记录", text)

    def test_period_without_pixel_products_uses_field_means_only(self) -> None:
        from app.reports.biweekly.offline_render import (
            build_period_facts,
            calendar_from_plan,
            render_facts,
            scene_rows_from_products,
        )

        facts, status = build_period_facts(
            land_id=LAND,
            calendar=calendar_from_plan(LAND, _smart_plan_rows()),
            season_id="2025/26",
            period_id="16",
            scene_rows=scene_rows_from_products(_real_shaped_products()),
            observation_source="parcel_scene_products",
        )
        with tempfile.TemporaryDirectory() as tmp:
            row = render_facts(facts, status, tmp, products=[])
            self.assertEqual(row["status"], "ready")
            self.assertEqual(row["pixel_dates"], [])
            self.assertEqual(row["mean_only_dates"], ["2026-05-06", "2026-05-14"])
            self.assertTrue(any(item.startswith("田里哪一块不一样") for item in row["not_provided"]))
            self.assertEqual(list((Path(tmp) / "figures").glob("infield_*.png")), [])


class CommandLineTests(unittest.TestCase):
    def _run(self, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
        import os

        env = {**os.environ, **(env_extra or {})}
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=300
        )

    def test_render_command_is_isolated_from_services(self) -> None:
        """整个命令在禁用网络的子进程里完成，且不加载任何连接服务的模块。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _write_inputs(root / "in")
            probe = (
                "import json, socket, sys, runpy\n"
                "def _blocked(*a, **k):\n"
                "    raise AssertionError('offline run opened a network connection')\n"
                "socket.socket.connect = _blocked\n"
                "socket.create_connection = _blocked\n"
                f"sys.argv = [{str(SCRIPT)!r}, 'render', '--land-id', {LAND!r}, '--plan', {str(paths['plan'])!r},\n"
                f"            '--products', {str(paths['products'])!r}, '--weather', {str(paths['weather'])!r},\n"
                f"            '--period', '16', '--as-of', '2026-05-20', '--out', {str(root / 'out')!r}]\n"
                "try:\n"
                "    runpy.run_path(sys.argv[0], run_name='__main__')\n"
                "except SystemExit as exc:\n"
                "    code = exc.code\n"
                "service = ('sqlalchemy', 'psycopg2', 'asyncpg', 'redis', 'celery', 'oss2', 'httpx', 'pika',\n"
                "           'agric_satellite_analysis_common.settings', 'agric_satellite_analysis_common.internal_api',\n"
                "           'agric_satellite_analysis_common.storage', 'app.core.config', 'app.worker', 'app.tasks')\n"
                "loaded = sorted(name for name in service if name in sys.modules)\n"
                "print('PROBE ' + json.dumps({'code': code, 'loaded': loaded}))\n"
            )
            import os

            env = {
                **os.environ,
                "DATABASE_URL": "postgresql://should-not-be-used/x",
                "REDIS_URL": "redis://should-not-be-used:6379/0",
                "API_BASE_URL": "http://should-not-be-used",
                "INTERNAL_API_TOKEN": "should-not-be-used",
            }
            done = subprocess.run(
                [sys.executable, "-c", probe], capture_output=True, text=True, env=env, timeout=300
            )
            self.assertEqual(done.returncode, 0, msg=done.stderr[-2000:])
            line = next(row for row in done.stdout.splitlines() if row.startswith("PROBE "))
            result = json.loads(line[len("PROBE ") :])
            self.assertEqual(result, {"code": 0, "loaded": []})
            self.assertIn("已从本进程环境中移除服务连接配置", done.stderr)
            self.assertNotIn("should-not-be-used", done.stdout + done.stderr)

            manifest = json.loads((root / "out" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["land_id"], LAND)
            self.assertEqual(manifest["observation_source"], "parcel_product_json")
            self.assertEqual(manifest["renderer"], "generate_official_biweekly_pdf(offline=True)")
            row = manifest["seasons"][0]["periods"][0]
            self.assertEqual(row["status"], "ready")
            self.assertTrue((root / "out" / row["dir"] / row["artifact"]).is_file())

    def test_periods_command_needs_no_observation_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.json"
            plan.write_text(json.dumps(_smart_plan_rows(), ensure_ascii=False), encoding="utf-8")
            done = self._run("periods", "--land-id", LAND, "--plan", str(plan))
            self.assertEqual(done.returncode, 0, msg=done.stderr)
            listed = json.loads(done.stdout)
            self.assertEqual(listed["seasons"][0]["season_id"], "2025/26")
            self.assertEqual(len(listed["seasons"][0]["periods"]), 18)

            missing = self._run("periods", "--land-id", "T901", "--plan", str(plan))
            self.assertEqual(missing.returncode, 1)
            self.assertIn("未关联 Smart 种植方案", missing.stderr)

    def test_render_facts_command_uses_exported_facts(self) -> None:
        from app.reports.biweekly.offline_render import (
            build_period_facts,
            calendar_from_plan,
            scene_rows_from_products,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _write_inputs(root / "in")
            facts, status = build_period_facts(
                land_id=LAND,
                calendar=calendar_from_plan(LAND, _smart_plan_rows()),
                season_id="2025/26",
                period_id="14",
                scene_rows=scene_rows_from_products(_real_shaped_products()),
                observation_source="parcel_scene_products",
            )
            envelope = root / "inputs.json"
            envelope.write_text(
                json.dumps({"job_id": "bj-x", "error_code": None, "status": status, "facts": facts}, ensure_ascii=False),
                encoding="utf-8",
            )
            done = self._run(
                "render-facts", "--facts", str(envelope), "--products", str(paths["products"]), "--out", str(root / "out")
            )
            self.assertEqual(done.returncode, 0, msg=done.stderr[-2000:])
            manifest = json.loads((root / "out" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "ready")
            self.assertEqual(manifest["baseline_date"], "2026-04-04")
            self.assertTrue((root / "out" / manifest["artifact"]).is_file())

            failed = root / "failed.json"
            failed.write_text(json.dumps({"error_code": "SCENE_QUERY_FAILED", "facts": facts}), encoding="utf-8")
            refused = self._run("render-facts", "--facts", str(failed), "--out", str(root / "out2"))
            self.assertEqual(refused.returncode, 1)
            self.assertIn("SCENE_QUERY_FAILED", refused.stderr)


if __name__ == "__main__":
    unittest.main()
