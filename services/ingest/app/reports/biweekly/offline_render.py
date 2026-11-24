"""Render official biweekly PDFs for one real land from exported files.

本模块只读本机文件、只写输出目录：不连接数据库、Redis、对象存储和内部接口，
不下载影像和气象数据，不调用 Celery 任务，也不回写任何生产接口。

它与线上链路共用同一份代码：
    种植季与分期   plan_calendar.build_seasons_from_rows → iter_observation_periods
    景的采用规则   scene_rows.to_observation
    事实构建       TriggerService.prepare_generate_facts
    补充与渲染     generate_official_biweekly_pdf(offline=True)

所有数值都来自输入文件。输入缺什么，报告就少什么并写明原因，不补造数据：
    本期没有可用光学观测 → 只写证据受限简报（JSON），不出 PDF（与线上一致）
    没有气象行           → 不出气象章节
    没有像元级产品       → 只用田块均值，不出田内分布图
    周边农田对照         → 离线不取真彩色影像，本节不提供
"""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from agric_satellite_analysis_common.biweekly.calendar import iter_periods
from agric_satellite_analysis_common.biweekly.models import ReportError
from agric_satellite_analysis_common.biweekly.plan_calendar import (
    SmartPlantingCatalog,
    build_seasons_from_rows,
    iter_observation_periods,
)
from agric_satellite_analysis_common.biweekly.scene_rows import (
    as_day,
    as_sensor,
    eligible_observations,
    radar_count,
    to_observation,
    weather_row,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
ANTECEDENT_DAYS = 30
# 线上从对象存储补读像元产品时，除本期观测日外还回看 28 天，用于配对上一期像元。
PRODUCT_LOOKBACK_DAYS = 28
SCENE_COLUMNS = (
    "land_id",
    "date",
    "sensor",
    "scene_id",
    "ndvi_avg",
    "evi_avg",
    "ndre_avg",
    "ndmi_avg",
    "cloud_cover",
    "parcel_cloud_cover_pct",
    "vv_avg",
    "vh_avg",
)
WEATHER_DB_COLUMNS = (
    "temperature_2m_min",
    "temperature_2m_max",
    "precipitation_sum",
    "et0_fao_mm",
)
WEATHER_REPORT_COLUMNS = ("min_c", "max_c", "precipitation_mm", "et0_mm")
NON_OBSERVED_KINDS = {"demo", "mock"}


class OfflineInputError(Exception):
    """An input file is missing, malformed or belongs to another land."""


# ---------------------------------------------------------------- file reading


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and value.strip() in {"", "NULL", "null", "\\N"}:
        return None
    return value


def load_rows(path: str | Path) -> list[dict[str, Any]]:
    """Rows from a JSON array, a JSON object holding ``rows``, or a CSV with a header.

    CSV 的空单元格、NULL、\\N 一律读成 None（缺测），不读成 0 或空字符串。
    """
    source = Path(path)
    if not source.is_file():
        raise OfflineInputError(f"找不到输入文件：{source}")
    if source.suffix.lower() == ".csv":
        with source.open(encoding="utf-8-sig", newline="") as handle:
            return [
                {key: _blank_to_none(value) for key, value in row.items() if key}
                for row in csv.DictReader(handle)
            ]
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise OfflineInputError(f"{source} 不是合法的 JSON：{exc}") from exc
    if isinstance(payload, dict):
        for key in ("rows", "data", "items"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise OfflineInputError(f"{source} 应为行对象的数组（或含 rows 数组的对象）")
    return payload


def load_json(path: str | Path) -> Any:
    source = Path(path)
    if not source.is_file():
        raise OfflineInputError(f"找不到输入文件：{source}")
    try:
        return json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise OfflineInputError(f"{source} 不是合法的 JSON：{exc}") from exc


def _same_land(value: Any, land_id: str) -> bool:
    return str(value).strip() == str(land_id).strip()


# ---------------------------------------------------------------- plan → seasons


def _live_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop rows flagged deleted, as the Smart loader's SQL does (del_flag = '0' or NULL)."""
    kept = []
    for row in rows:
        flag = row.get("del_flag")
        if flag is None or str(flag).strip() in {"", "0"}:
            kept.append(row)
    return kept


def _period_identity(rows: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [
        (
            str(row.get("period_id")),
            as_day(row.get("start")),
            as_day(row.get("end_exclusive")),
        )
        for row in rows
    ]


def _season_from_api(land_id: str, season: dict[str, Any]) -> dict[str, Any]:
    plan = season.get("native_plan")
    if not isinstance(plan, dict) or not plan.get("intervals"):
        raise OfflineInputError("方案文件中的种植季没有 native_plan（本田 Smart 方案），无法推导分期")
    if plan.get("land_id") is not None and not _same_land(plan["land_id"], land_id):
        raise OfflineInputError(
            f"方案文件中的 native_plan 属于地块 {plan['land_id']}，不是 {land_id}；不会改用其他地块的方案"
        )
    start = as_day(season.get("start") or plan.get("start") or "")
    end_inclusive = as_day(season.get("end_inclusive") or plan.get("end_inclusive") or "")
    season_id = str(season.get("season_id") or "").strip()
    if not (start and end_inclusive and season_id):
        raise OfflineInputError("方案文件中的种植季缺少 season_id / start / end_inclusive")
    # 新导出从 S1 起算。已经按整季窗口导出的历史文件仍按原分期核对，不改写旧期次。
    derived = iter_observation_periods(plan)
    legacy = iter_periods(start, end_inclusive)
    supplied = season.get("periods")
    chosen = derived
    if supplied:
        got = _period_identity(supplied)
        if got == _period_identity(derived):
            chosen = derived
        elif got == _period_identity(legacy):
            chosen = legacy
        else:
            raise OfflineInputError(
                f"种植季 {season_id} 的分期与正式推导不一致，请重新导出方案文件"
            )
    periods = [
        {
            **item,
            "snapshot": None,
            "pack_pdf": None,
            "optical_descriptive_days": 0,
            "s1_products": 0,
            "has_eligible_optics": False,
            "plan_status": "smart_plant_task",
            "label": f"第{item['period_id']}期",
        }
        for item in chosen
    ]
    return {
        "season_id": season_id,
        "label": season.get("label") or f"{season_id} 种植季",
        "land_id": str(land_id),
        "start": start,
        "end_inclusive": end_inclusive,
        "end_exclusive": (date.fromisoformat(end_inclusive) + timedelta(days=1)).isoformat(),
        "cadence_days": 14,
        "native_plan": plan,
        "periods": periods,
    }


def calendar_from_plan(land_id: str, payload: Any) -> dict[str, Any]:
    """Seasons and periods for one land from an exported plan file.

    接受三种来源。新方案的分期从 S1 起算；已按整季窗口导出的历史文件保持原分期：
    1. Smart 原始行 {"batches": [...], "tasks": [...], "mapping_rows": [...]（可选）}
       → build_seasons_from_rows（与接口机读取 Smart 后调用的是同一个函数）；
    2. 接口 GET /v1/lands/{land_id}/biweekly/seasons 的返回（含 seasons[].native_plan）；
    3. 接口 GET /v1/lands/{land_id}/biweekly/periods?season=… 的返回（含 native_plan）。
    """
    land = str(land_id)
    if not isinstance(payload, dict):
        raise OfflineInputError("方案文件应为 JSON 对象")
    if "batches" in payload or "tasks" in payload:
        batches = _live_rows(payload.get("batches") or [])
        tasks = _live_rows(payload.get("tasks") or [])
        calendar = build_seasons_from_rows(
            land, batches, tasks, mapping_rows=payload.get("mapping_rows") or None
        )
        calendar["plan_input"] = "smart_rows"
        return calendar
    if payload.get("land_id") is not None and not _same_land(payload["land_id"], land):
        raise OfflineInputError(
            f"方案文件属于地块 {payload['land_id']}，不是 {land}；不会改用其他地块的方案"
        )
    if isinstance(payload.get("seasons"), list):
        seasons = [_season_from_api(land, row) for row in payload["seasons"]]
        source = "api_seasons"
    elif payload.get("season_id") and payload.get("native_plan"):
        seasons = [_season_from_api(land, payload)]
        source = "api_periods"
    else:
        raise OfflineInputError(
            "方案文件无法识别：需要 Smart 原始行（batches/tasks），或接口 seasons / periods 的返回"
        )
    return {
        "land_id": land,
        "seasons": seasons,
        "plan_available": bool(seasons),
        "plan_status": "smart_plant_task" if seasons else "missing",
        "plan_source": "smart_agric.agriculture_plant_task",
        "plan_input": source,
    }


# ---------------------------------------------------------------- products and scenes


def _product_file_rank(name: str) -> int:
    """线上只读取 {日期}_S2.json 与 {日期}_S1.json；同名规则的文件优先。"""
    stem = name[:-5] if name.lower().endswith(".json") else name
    parts = stem.split("_", 1)
    if len(parts) == 2 and parts[1] in {"S2", "S1"} and len(parts[0]) == 10:
        return 0
    return 1


def load_products(products_dir: str | Path, land_id: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parcel product JSON for this land, plus notes about files that were not used.

    产品的 land_id 必须等于目标地块，否则直接报错，不用其他地块的数据代替。
    去云派生产品（*_S2_decloud.json）不读取，线上出报同样不读取。
    """
    from app.reports.biweekly.enrichment import normalize_product

    directory = Path(products_dir)
    if not directory.is_dir():
        raise OfflineInputError(f"找不到像元产品目录：{directory}")
    notes: list[str] = []
    chosen: dict[tuple[str, str], tuple[int, str, dict[str, Any]]] = {}
    for path in sorted(directory.glob("*.json")):
        name = path.name
        if name.startswith("_") or name.endswith("_index.json"):
            continue
        if "decloud" in name.lower():
            notes.append(f"{name}：去云派生产品，出报不读取，已跳过")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OfflineInputError(f"{path} 读取失败：{type(exc).__name__}") from exc
        if not isinstance(payload, dict) or not payload.get("date"):
            notes.append(f"{name}：不是地块场景产品（缺少 date），已跳过")
            continue
        if str(payload.get("data_kind") or "").lower() in NON_OBSERVED_KINDS:
            # 样稿生成器写出的产品带有示例标记；它们不是观测，不能经本入口出成真实地块的报告。
            raise OfflineInputError(
                f"{name} 标记为示例数据（data_kind={payload['data_kind']}），不能作为真实地块数据出报"
            )
        if payload.get("land_id") is None:
            raise OfflineInputError(f"{name} 缺少 land_id，无法确认属于地块 {land_id}")
        if not _same_land(payload["land_id"], land_id):
            raise OfflineInputError(
                f"{name} 属于地块 {payload['land_id']}，不是 {land_id}；不会用其他地块的数据代替"
            )
        product = normalize_product(payload)
        key = (as_day(product["date"]), as_sensor(product.get("sensor") or "S2"))
        rank = _product_file_rank(name)
        if key in chosen:
            held_rank, held_name, _ = chosen[key]
            if rank == held_rank:
                raise OfflineInputError(
                    f"{held_name} 与 {name} 是同一天、同一传感器的两个产品，请只保留一个"
                )
            if rank > held_rank:
                notes.append(f"{name}：与 {held_name} 同日同传感器，已按线上命名规则采用后者")
                continue
            notes.append(f"{held_name}：与 {name} 同日同传感器，已按线上命名规则采用后者")
        chosen[key] = (rank, name, product)
    products = [item[2] for _, item in sorted(chosen.items())]
    return products, notes


def scene_rows_from_products(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One scene row per product, with the columns the report reads from parcel_scene_products.

    场景产品 JSON 的顶层字段就是入库到 parcel_scene_products 的同名列，这里原样取出，不重算。
    """
    rows = []
    for product in products:
        row = {key: product.get(key) for key in SCENE_COLUMNS}
        row["date"] = as_day(product["date"])
        row["sensor"] = as_sensor(product.get("sensor") or "S2")
        rows.append(row)
    return rows


def check_scene_rows(rows: list[dict[str, Any]], land_id: str) -> list[dict[str, Any]]:
    checked = []
    for index, row in enumerate(rows, start=1):
        if not row.get("date") or not row.get("sensor"):
            raise OfflineInputError(f"景清单第 {index} 行缺少 date 或 sensor")
        if row.get("land_id") is not None and not _same_land(row["land_id"], land_id):
            raise OfflineInputError(
                f"景清单第 {index} 行属于地块 {row['land_id']}，不是 {land_id}；不会用其他地块的数据代替"
            )
        checked.append(row)
    checked.sort(key=lambda row: (as_day(row["date"]), as_sensor(row["sensor"]), str(row.get("scene_id") or "")))
    return checked


def observations_between(
    rows: list[dict[str, Any]], start: str, end_exclusive: str, *, source: str
) -> list[dict[str, Any]]:
    """Same window as the API scene query: start <= date < end_exclusive."""
    return [
        to_observation(row, source=source)
        for row in rows
        if start <= as_day(row["date"]) < end_exclusive
    ]


def last_optical_observations(
    rows: list[dict[str, Any]], periods: list[dict[str, Any]], period_id: str, *, source: str
) -> list[dict[str, Any]]:
    """Rows of the most recent earlier period that has eligible optics (else none).

    与接口机 _last_optical_observations 相同：从上一期起逐期向前找，取第一个有可用光学的期。
    """
    ids = [str(row["period_id"]) for row in periods]
    try:
        index = ids.index(str(period_id))
    except ValueError:
        return []
    for prior in reversed(periods[:index]):
        found = observations_between(rows, prior["start"], prior["end_exclusive"], source=source)
        if eligible_observations(found):
            return found
    return []


# ---------------------------------------------------------------- weather


def load_weather(path: str | Path, land_id: str) -> list[dict[str, Any]]:
    """Daily weather rows in report shape, from a weather_daily export.

    接受数据库列名（temperature_2m_min / temperature_2m_max / precipitation_sum / et0_fao_mm）
    或报告字段名（min_c / max_c / precipitation_mm / et0_mm）。缺测保持 None。
    """
    out: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(load_rows(path), start=1):
        if not row.get("date"):
            raise OfflineInputError(f"气象文件第 {index} 行缺少 date")
        if row.get("land_id") is not None and not _same_land(row["land_id"], land_id):
            raise OfflineInputError(
                f"气象文件第 {index} 行属于地块 {row['land_id']}，不是 {land_id}；不会用其他地块的数据代替"
            )
        if any(key in row for key in WEATHER_DB_COLUMNS):
            item = weather_row(row)
        elif any(key in row for key in WEATHER_REPORT_COLUMNS):
            item = weather_row(
                {
                    "date": row["date"],
                    "temperature_2m_min": row.get("min_c"),
                    "temperature_2m_max": row.get("max_c"),
                    "precipitation_sum": row.get("precipitation_mm"),
                    "et0_fao_mm": row.get("et0_mm"),
                }
            )
        else:
            raise OfflineInputError(f"气象文件第 {index} 行没有可识别的气温、降水或蒸散量列")
        if item["date"] in out:
            raise OfflineInputError(f"气象文件中 {item['date']} 出现多行，请每天只保留一行")
        out[item["date"]] = item
    return [out[day] for day in sorted(out)]


def weather_payload(weather: list[dict[str, Any]] | None, period: dict[str, Any]) -> dict[str, Any]:
    """Period and 30-day antecedent weather, assembled as the API does for the generate job."""
    from app.reports.biweekly.weather_fetch import summarize_weather_rows

    if weather is None:
        return {"weather_daily": None, "weather_period": None, "weather_antecedent": None}
    start = period["start"]
    end_inclusive = period.get("end_inclusive") or period["end_exclusive"]
    first = (date.fromisoformat(start) - timedelta(days=ANTECEDENT_DAYS)).isoformat()
    period_rows = [row for row in weather if start <= row["date"] <= end_inclusive]
    antecedent_rows = [row for row in weather if first <= row["date"] < start]
    daily = list(period_rows)
    if period_rows and antecedent_rows:
        daily = antecedent_rows + period_rows
    return {
        "weather_daily": daily,
        "weather_period": summarize_weather_rows(period_rows),
        "weather_antecedent": summarize_weather_rows(antecedent_rows),
    }


# ---------------------------------------------------------------- facts


def build_period_facts(
    *,
    land_id: str,
    calendar: dict[str, Any],
    season_id: str,
    period_id: str,
    scene_rows: list[dict[str, Any]],
    observation_source: str,
    weather: list[dict[str, Any]] | None = None,
    land_name: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Official facts for one period, built by TriggerService as the API does.

    任务载荷的字段与接口机 _enrich_payload 一致；这里不创建任务、不落任务文件到业务目录。
    """
    from agric_satellite_analysis_common.biweekly.trigger import TriggerService

    catalog = SmartPlantingCatalog(calendar)
    period = catalog.period(land_id, season_id, period_id)
    periods = catalog.periods(land_id, season_id)
    observations = observations_between(
        scene_rows, period["start"], period["end_exclusive"], source=observation_source
    )
    payload = {
        "observations_override": observations,
        "prior_observations_override": last_optical_observations(
            scene_rows, periods, period_id, source=observation_source
        ),
        "observation_source": observation_source,
        "land_name": land_name,
        "s1_count": radar_count(observations),
        "native_plan": catalog.native_plan(land_id, season_id),
        **weather_payload(weather, period),
    }
    job = {
        "job_id": f"offline-{land_id}-{period_id}",
        "land_id": str(land_id),
        "season_id": season_id,
        "period_id": str(period_id),
        "payload": payload,
    }
    with tempfile.TemporaryDirectory(prefix="biweekly-offline-") as work:
        service = TriggerService(Path(work), catalog, auto_run=False)
        return service.prepare_generate_facts(job)


def products_for_period(products: list[dict[str, Any]], facts: dict[str, Any]) -> list[dict[str, Any]]:
    """The pixel products this period may use.

    线上按日期从对象存储读取：本期观测日，加上本期开始前 28 天至本期结束。这里取同样的日期范围，
    另加 facts 已列为对比基准的观测日，使相隔超过 28 天的基准日也有像元级统计。
    更早的产品不传入，避免把无关日期带进对比。
    """
    period = facts.get("period") or {}
    start = as_day(period.get("start") or "")
    end_exclusive = as_day(period.get("end_exclusive") or "")
    if not start or not end_exclusive:
        return []
    first = (date.fromisoformat(start) - timedelta(days=PRODUCT_LOOKBACK_DAYS)).isoformat()
    cited = {
        as_day(row.get("date"))
        for key in ("observations", "prior_observations")
        for row in facts.get(key) or []
        if isinstance(row, dict) and row.get("date")
    }
    return [
        product
        for product in products
        if first <= as_day(product["date"]) < end_exclusive or as_day(product["date"]) in cited
    ]


# ---------------------------------------------------------------- rendering


def pdf_filename(facts: dict[str, Any]) -> str:
    """Same download name as the official task."""
    period = facts.get("period") or {}
    return f"种植双周报_{facts.get('land_id')}_{period.get('start')}_{period.get('end_inclusive')}.pdf"


def _degradations(filled: dict[str, Any]) -> list[str]:
    from app.reports.biweekly import report_text

    return [item for item in report_text.omitted_items(filled) if "未经地面标定" not in item]


def render_facts(
    facts: dict[str, Any],
    status: str,
    out_dir: str | Path,
    *,
    products: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write one period's artifact and return what was produced.

    status == "ready"            → 正式 PDF（generate_official_biweekly_pdf，offline=True）
    status == "evidence_limited" → 证据受限简报 evidence-limited.json，不出 PDF
    """
    from app.reports.biweekly.generate import generate_official_biweekly_pdf

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    period = facts.get("period") or {}
    result: dict[str, Any] = {
        "period_id": period.get("period_id"),
        "start": period.get("start"),
        "end_inclusive": period.get("end_inclusive"),
        "status": status,
        "optical_days": list(facts.get("optical_days") or []),
        "rejected_optical": len(facts.get("rejected_optical") or []),
        "radar_scenes": int(facts.get("s1_count") or 0),
    }
    facts_path = out / "facts.json"
    facts_path.write_text(
        json.dumps(facts, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    result["facts"] = facts_path.name
    if status == "evidence_limited":
        brief = out / "evidence-limited.json"
        brief.write_text(json.dumps(facts, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        result["artifact"] = brief.name
        result["artifact_kind"] = "evidence_limited_brief"
        result["note"] = "本期没有可用光学观测，只给出证据受限简报，不是正式 PDF。"
        return result
    if status != "ready":
        raise OfflineInputError(f"无法识别的出报状态：{status}")

    used = list(products or [])
    pdf = out / pdf_filename(facts)
    sidecar = out / "facts-enriched.json"
    generate_official_biweekly_pdf(
        facts,
        pdf,
        figures_dir=out / "figures",
        products=used,
        facts_sidecar=sidecar,
        offline=True,
    )
    filled = json.loads(sidecar.read_text(encoding="utf-8"))
    quality = filled.get("data_quality") or {}
    comparison = filled.get("comparison") or {}
    result.update(
        {
            "artifact": pdf.name,
            "artifact_kind": "official_visual_artifact",
            "sha256": sha256_file(pdf),
            "pixel_dates": quality.get("pixel_dates") or [],
            "mean_only_dates": quality.get("mean_only_dates") or [],
            "baseline_date": comparison.get("baseline_date"),
            "phase": (filled.get("phase") or {}).get("display_phase"),
            "primary_metrics": (filled.get("report_metrics") or {}).get("primary") or [],
            "not_provided": _degradations(filled),
        }
    )
    return result


def period_dir_name(period: dict[str, Any]) -> str:
    return f"period-{int(period['period_id']):02d}_{period['start']}_{period['end_inclusive']}"


def season_dir_name(season_id: str) -> str:
    return "season-" + "".join(ch if ch.isalnum() or ch in "-." else "-" for ch in season_id)


def today_shanghai() -> str:
    return datetime.now(SHANGHAI).date().isoformat()


def render_land(
    *,
    land_id: str,
    calendar: dict[str, Any],
    scene_rows: list[dict[str, Any]],
    observation_source: str,
    products: list[dict[str, Any]],
    out_dir: str | Path,
    weather: list[dict[str, Any]] | None = None,
    land_name: str | None = None,
    seasons: list[str] | None = None,
    period_ids: list[str] | None = None,
    as_of: str | None = None,
    include_current: bool = False,
) -> dict[str, Any]:
    """Every period of every selected season: PDF, evidence-limited brief, or skipped."""
    out = Path(out_dir)
    today = as_of or today_shanghai()
    wanted_seasons = set(seasons or [])
    known = {row["season_id"] for row in calendar.get("seasons") or []}
    unknown = wanted_seasons - known
    if unknown:
        raise OfflineInputError(f"方案中没有种植季 {sorted(unknown)}；可选：{sorted(known)}")
    report: dict[str, Any] = {"land_id": str(land_id), "as_of": today, "seasons": []}
    for season in calendar.get("seasons") or []:
        season_id = season["season_id"]
        if wanted_seasons and season_id not in wanted_seasons:
            continue
        entry = {
            "season_id": season_id,
            "start": season["start"],
            "end_inclusive": season["end_inclusive"],
            "periods": [],
        }
        for period in season["periods"]:
            if period_ids and str(period["period_id"]) not in {str(item) for item in period_ids}:
                continue
            row: dict[str, Any] = {
                "period_id": period["period_id"],
                "start": period["start"],
                "end_inclusive": period["end_inclusive"],
            }
            if period["start"] > today:
                # 尚未开始的分期没有观测可言，不写成「本期无可用光学」。
                row.update(status="not_started", note=f"截至 {today} 本期尚未开始，未生成")
            elif period["end_inclusive"] >= today and not include_current:
                row.update(
                    status="in_progress",
                    note=f"截至 {today} 本期尚未结束，未生成；需要时加 --include-current",
                )
            else:
                target = out / season_dir_name(season_id) / period_dir_name(period)
                try:
                    facts, status = build_period_facts(
                        land_id=land_id,
                        calendar=calendar,
                        season_id=season_id,
                        period_id=period["period_id"],
                        scene_rows=scene_rows,
                        observation_source=observation_source,
                        weather=weather,
                        land_name=land_name,
                    )
                    row = render_facts(
                        facts, status, target, products=products_for_period(products, facts)
                    )
                    row["dir"] = str(target.relative_to(out))
                except (ReportError, OfflineInputError) as exc:
                    row.update(status="failed", error=getattr(exc, "code", None) or str(exc))
                except Exception as exc:  # noqa: BLE001 — 单期失败记入清单，其余各期照常生成
                    row.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            entry["periods"].append(row)
        report["seasons"].append(entry)
    return report


def facts_from_export(payload: Any) -> tuple[dict[str, Any], str]:
    """Facts and status from an exported internal ``/inputs`` response, or bare facts."""
    if not isinstance(payload, dict):
        raise OfflineInputError("facts 文件应为 JSON 对象")
    if isinstance(payload.get("facts"), dict):
        if payload.get("error_code"):
            raise OfflineInputError(f"facts 文件带有错误码 {payload['error_code']}，不能出报")
        facts = dict(payload["facts"])
        status = str(payload.get("status") or (facts.get("generate") or {}).get("status") or "")
    else:
        facts = dict(payload)
        status = str((facts.get("generate") or {}).get("status") or "")
    if not facts.get("land_id") or not (facts.get("period") or {}).get("start"):
        raise OfflineInputError("facts 文件缺少 land_id 或 period")
    if status not in {"ready", "evidence_limited"}:
        raise OfflineInputError("facts 文件没有可识别的出报状态（ready / evidence_limited）")
    return facts, status


def export_windows(calendar: dict[str, Any]) -> list[dict[str, Any]]:
    """Per season: the periods, and the date ranges each export must cover."""
    out = []
    for season in calendar.get("seasons") or []:
        begin = date.fromisoformat(season["start"])
        end = season["end_inclusive"]
        out.append(
            {
                "season_id": season["season_id"],
                "start": season["start"],
                "end_inclusive": end,
                "export_ranges": {
                    "scenes": {"date_from": season["start"], "date_to_inclusive": end},
                    # 像元产品多取本季开始前 28 天，供第一期配对上一次观测（没有也可以）。
                    "products": {
                        "date_from": (begin - timedelta(days=PRODUCT_LOOKBACK_DAYS)).isoformat(),
                        "date_to_inclusive": end,
                    },
                    # 气象多取本季开始前 30 天，供第一期说明前期降水（没有也可以）。
                    "weather": {
                        "date_from": (begin - timedelta(days=ANTECEDENT_DAYS)).isoformat(),
                        "date_to_inclusive": end,
                    },
                },
                "periods": [
                    {
                        "period_id": row["period_id"],
                        "start": row["start"],
                        "end_inclusive": row["end_inclusive"],
                        "days": row["days"],
                        "partial": row["partial"],
                    }
                    for row in season["periods"]
                ],
            }
        )
    return out
