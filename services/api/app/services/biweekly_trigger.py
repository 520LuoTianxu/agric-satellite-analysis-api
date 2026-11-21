"""Factory for the API-machine biweekly TriggerService."""

from __future__ import annotations

from pathlib import Path

from agric_satellite_analysis_common.biweekly.calendar import iter_periods, planting_season_label
from agric_satellite_analysis_common.biweekly.catalog import LocalPackCatalog
from agric_satellite_analysis_common.biweekly.models import ReportError
from agric_satellite_analysis_common.biweekly.trigger import TriggerService

from app.core.config import settings
from app.services.biweekly_scenes import is_maize_crop

DEFAULT_PACK_ROOT = Path("/workspace/reports/field6417-2025-2026")
DEFAULT_WORK_DIR = Path("/tmp/biweekly-api-jobs")

WHEAT_SEASON_START = "2025-11-01"
WHEAT_SEASON_END_INCLUSIVE = "2026-06-30"


class CalendarCatalog:
    """14-day wheat calendar for lands that are not the local 6417 pack."""

    def list_seasons(self, land_id: str) -> list[dict]:
        label = planting_season_label(WHEAT_SEASON_START, WHEAT_SEASON_END_INCLUSIVE)
        return [
            {
                "season_id": label,
                "label": f"{label} 种植季",
                "land_id": str(land_id),
                "crop": "winter_wheat",
                "crop_name": "冬小麦",
                "start": WHEAT_SEASON_START,
                "end_inclusive": WHEAT_SEASON_END_INCLUSIVE,
                "end_exclusive": "2026-07-01",
                "cadence_days": 14,
                "note": "冬小麦默认种植季窗。不是玉米季，也不把玉米假数据写入该地块。",
            }
        ]

    def periods(self, land_id: str, season_id: str) -> list[dict]:
        seasons = {row["season_id"]: row for row in self.list_seasons(land_id)}
        season = seasons.get(season_id)
        if season is None:
            raise ReportError("SEASON_NOT_FOUND")
        rows = []
        for item in iter_periods(season["start"], season["end_inclusive"]):
            rows.append(
                {
                    **item,
                    "snapshot": None,
                    "pack_pdf": None,
                    "optical_descriptive_days": 0,
                    "s1_products": 0,
                    "has_eligible_optics": False,
                    "plan_status": "unknown",
                }
            )
        return rows

    def period(self, land_id: str, season_id: str, period_id: str) -> dict:
        for row in self.periods(land_id, season_id):
            if row["period_id"] == str(period_id):
                return row
        raise ReportError("PERIOD_NOT_FOUND")

    def load_snapshot(self, relative: str) -> dict:
        raise ReportError("SNAPSHOT_NOT_FOUND")

    def v8_optical_metrics(self) -> dict | None:
        return None

    def v8_pdf(self) -> Path | None:
        return None

    def v8_prior_period(self) -> dict | None:
        return None


class CompositeCatalog:
    def __init__(self, local: LocalPackCatalog | None, calendar: CalendarCatalog):
        self.local = local
        self.calendar = calendar

    def list_seasons(self, land_id: str) -> list[dict]:
        if self.local is not None:
            try:
                return self.local.list_seasons(land_id)
            except ReportError as exc:
                if exc.code != "LAND_NOT_IN_LOCAL_CATALOG":
                    raise
        return self.calendar.list_seasons(land_id)

    def periods(self, land_id: str, season_id: str) -> list[dict]:
        if self.local is not None:
            try:
                return self.local.periods(land_id, season_id)
            except ReportError as exc:
                if exc.code not in {"LAND_NOT_IN_LOCAL_CATALOG", "SEASON_NOT_FOUND"}:
                    raise
        return self.calendar.periods(land_id, season_id)

    def period(self, land_id: str, season_id: str, period_id: str) -> dict:
        if self.local is not None:
            try:
                return self.local.period(land_id, season_id, period_id)
            except ReportError as exc:
                if exc.code not in {"LAND_NOT_IN_LOCAL_CATALOG", "SEASON_NOT_FOUND", "PERIOD_NOT_FOUND"}:
                    raise
        return self.calendar.period(land_id, season_id, period_id)

    def load_snapshot(self, relative: str) -> dict:
        if self.local is not None:
            return self.local.load_snapshot(relative)
        raise ReportError("SNAPSHOT_NOT_FOUND")

    def v8_optical_metrics(self) -> dict | None:
        return self.local.v8_optical_metrics() if self.local is not None else None

    def v8_pdf(self) -> Path | None:
        return self.local.v8_pdf() if self.local is not None else None

    def v8_prior_period(self) -> dict | None:
        return self.local.v8_prior_period() if self.local is not None else None


def _local_pack() -> LocalPackCatalog | None:
    pack_root = Path(settings.biweekly_pack_root or DEFAULT_PACK_ROOT)
    if not (pack_root / "manifest.json").is_file():
        return None
    try:
        return LocalPackCatalog(pack_root=pack_root)
    except ReportError:
        return None


def build_trigger_service() -> TriggerService:
    work_dir = Path(settings.biweekly_work_dir or DEFAULT_WORK_DIR)
    work_dir.mkdir(parents=True, exist_ok=True)
    return TriggerService(
        work_dir=work_dir,
        catalog=CompositeCatalog(_local_pack(), CalendarCatalog()),
        auto_run=True,
    )


def reject_maize(crop_type: str | None) -> bool:
    return is_maize_crop(crop_type)


def annotate_dispatch(job: dict, dispatch: dict | None = None) -> dict:
    out = dict(job)
    result = dict(out.get("result") or {})
    planned = (out.get("payload") or {}).get("planned_batch") or result.get("planned_batch")
    if dispatch:
        result.update(dispatch)
        result["dispatched_to_production"] = bool(dispatch.get("dispatched"))
        result["satellite_batch_job_ids"] = list(dispatch.get("job_ids") or [])
        if planned:
            result["planned_batch"] = planned
        out["dispatched_to_production"] = bool(dispatch.get("dispatched"))
        out["satellite_batch_job_ids"] = list(dispatch.get("job_ids") or [])
    elif "dispatched_to_production" not in result:
        result["dispatched_to_production"] = False
        out["dispatched_to_production"] = False
    out["result"] = result
    if planned:
        out["planned_batch"] = planned
    return out
