"""Planting seasons and 14-day periods from one land's Smart wheat plan rows.

Pure builder: no database, no network. The API loads the rows from Smart and
calls this module; offline tooling calls it with exported rows, so both derive
the same season windows, period numbering and stage intervals.

种植季窗口取本田 Smart 小麦种植任务（agriculture_plant_task）的最早计划开始日
到最晚计划结束日，再按 14 天一期切分。没有方案时返回空的、带说明的状态，
不会改用其他地块的方案。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

from .calendar import iter_periods, planting_season_label
from .models import ReportError
from .phase_policy import CROP_ID, CROP_NAME, MAPPING_VERSION, resolve_wheat_stage

MISSING_NOTE = "本田未关联 Smart 种植方案，未列出种植季。不会改用其他地块的方案。"


def as_plan_day(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    return date.fromisoformat(text[:10])


def empty_calendar(*, land_id: str, plan_status: str, note: str) -> dict[str, Any]:
    return {
        "land_id": str(land_id),
        "seasons": [],
        "plan_available": False,
        "plan_status": plan_status,
        "plan_source": "smart_agric",
        "crop_id": CROP_ID,
        "crop_name": CROP_NAME,
        "auto_publish": False,
        "note": note,
    }


class SmartPlantingCatalog:
    """In-memory catalog built from one land's Smart wheat batches and plant tasks."""

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.land_id = str(payload.get("land_id") or "")

    @property
    def plan_available(self) -> bool:
        return bool(self.payload.get("plan_available") and self.payload.get("seasons"))

    def list_seasons(self, land_id: str) -> list[dict]:
        if str(land_id) != self.land_id:
            return []
        return list(self.payload.get("seasons") or [])

    def periods(self, land_id: str, season_id: str) -> list[dict]:
        season = self._season(land_id, season_id)
        return list(season.get("periods") or [])

    def period(self, land_id: str, season_id: str, period_id: str) -> dict:
        for row in self.periods(land_id, season_id):
            if row["period_id"] == str(period_id):
                return row
        raise ReportError("PERIOD_NOT_FOUND")

    def native_plan(self, land_id: str, season_id: str) -> dict | None:
        if not self.plan_available:
            return None
        season = self._season(land_id, season_id)
        plan = season.get("native_plan")
        return dict(plan) if plan else None

    def _season(self, land_id: str, season_id: str) -> dict:
        if str(land_id) != self.land_id:
            raise ReportError("SEASON_NOT_FOUND")
        for row in self.payload.get("seasons") or []:
            if row["season_id"] == season_id:
                return row
        raise ReportError("SEASON_NOT_FOUND")

    def load_snapshot(self, relative: str) -> dict:
        raise ReportError("SNAPSHOT_NOT_FOUND")

    def v8_optical_metrics(self) -> dict | None:
        return None

    def v8_pdf(self):
        return None

    def v8_prior_period(self) -> dict | None:
        return None


def build_seasons_from_rows(
    land_id: str,
    batches: Iterable[dict[str, Any]],
    tasks: Iterable[dict[str, Any]],
    *,
    mapping_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pure builder used by the MySQL loader, offline tooling and unit tests."""
    requested = str(land_id)
    tasks_by_plan: dict[str, list[dict[str, Any]]] = {}
    for raw in tasks:
        plan_id = str(raw.get("plan_id") or "")
        tasks_by_plan.setdefault(plan_id, []).append(raw)

    seasons: list[dict[str, Any]] = []
    seen_labels: dict[str, int] = {}
    for batch in batches:
        if str(batch.get("land_id")) != requested:
            # Defensive: never attach a row for a different parcel.
            continue
        if int(batch.get("crop_id") or 0) != CROP_ID:
            continue
        batch_id = str(batch["batch_id"])
        plan = plan_from_tasks(
            land_id=requested,
            batch=batch,
            rows=tasks_by_plan.get(batch_id) or [],
            mapping_rows=mapping_rows,
        )
        if not plan or not plan.get("start") or not plan.get("end_inclusive"):
            continue
        label = planting_season_label(plan["start"], plan["end_inclusive"])
        seen_labels[label] = seen_labels.get(label, 0) + 1
        season_id = label if seen_labels[label] == 1 else f"{label}-b{batch_id}"
        periods = []
        for item in iter_periods(plan["start"], plan["end_inclusive"]):
            periods.append(
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
            )
        seasons.append(
            {
                "season_id": season_id,
                "label": f"{season_id} 种植季",
                "land_id": requested,
                "crop": "wheat",
                "crop_id": CROP_ID,
                "crop_name": str(batch.get("crop_name") or CROP_NAME),
                "start": plan["start"],
                "end_inclusive": plan["end_inclusive"],
                "end_exclusive": plan["end_exclusive"],
                "cadence_days": 14,
                "batch_id": batch_id,
                "batch_name": batch.get("batch_name"),
                "scheme_handling_profile": batch.get("scheme_handling_profile"),
                "project_crop_season_id": batch.get("project_crop_season_id"),
                "plan_status": "smart_plant_task",
                "plan_label": "本田方案",
                "plan_source": "smart_agric.agriculture_plant_task",
                "mapping_version": MAPPING_VERSION,
                "native_plan": plan,
                "periods": periods,
                "note": "按本田 Smart 种植任务窗口列出种植季。生育期来自计划，不是卫星识别物候。",
            }
        )

    if not seasons:
        return empty_calendar(land_id=requested, plan_status="missing", note=MISSING_NOTE)
    return {
        "land_id": requested,
        "seasons": seasons,
        "plan_available": True,
        "plan_status": "smart_plant_task",
        "plan_source": "smart_agric.agriculture_plant_task",
        "crop_id": CROP_ID,
        "crop_name": CROP_NAME,
        "auto_publish": False,
        "note": "已读取本田 Smart 种植方案。默认不出报，需显式触发补数或生成。",
    }


def plan_from_tasks(
    *,
    land_id: str,
    batch: dict[str, Any],
    rows: list[dict[str, Any]],
    mapping_rows: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    intervals = []
    unmapped: list[str] = []
    days: list[date] = []
    for raw in rows:
        start = as_plan_day(raw.get("plan_start"))
        finish = as_plan_day(raw.get("plan_finish"))
        name = str(raw.get("task_name") or "").strip()
        if start is None or finish is None or not name:
            continue
        if finish < start:
            continue
        days.extend([start, finish])
        try:
            resolved = resolve_wheat_stage(name, mapping_rows)
        except ReportError:
            unmapped.append(name)
            continue
        intervals.append(
            {
                "stage_code": resolved["stage_code"],
                "source_stage_name": name,
                "start": start.isoformat(),
                "end_exclusive": (finish + timedelta(days=1)).isoformat(),
                "end_inclusive": finish.isoformat(),
                "mapping_status": resolved["status"],
                "phase_label": resolved.get("phase_label"),
                "phase_code": resolved.get("phase_code"),
                "tags": list(resolved.get("tags") or []),
                "plant_task_id": raw.get("plant_task_id"),
                "job_id": raw.get("job_id"),
            }
        )
    if not days:
        return None
    start = min(days)
    end_inclusive = max(days)
    return {
        "land_id": land_id,
        "batch_id": str(batch["batch_id"]),
        "batch_name": batch.get("batch_name"),
        "crop_id": CROP_ID,
        "crop_name": str(batch.get("crop_name") or CROP_NAME),
        "plan_label": "本田方案",
        "plan_source": "smart_agric.agriculture_plant_task",
        "phenology_basis": "PLAN_DERIVED_FROM_SMART_PLANT_TASK",
        "mapping_version": MAPPING_VERSION,
        "start": start.isoformat(),
        "end_inclusive": end_inclusive.isoformat(),
        "end_exclusive": (end_inclusive + timedelta(days=1)).isoformat(),
        "intervals": intervals,
        "unmapped_task_names": unmapped,
        "scheme_handling_profile": batch.get("scheme_handling_profile"),
    }
