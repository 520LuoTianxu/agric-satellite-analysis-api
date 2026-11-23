"""Factory for the API-machine biweekly TriggerService."""

from __future__ import annotations

from pathlib import Path

from agric_satellite_analysis_common.biweekly.catalog import LocalPackCatalog
from agric_satellite_analysis_common.biweekly.models import ReportError
from agric_satellite_analysis_common.biweekly.trigger import TriggerService

from app.core.config import settings
from app.services.biweekly_plans import SmartPlantingCatalog
from app.services.biweekly_scenes import is_maize_crop

DEFAULT_PACK_ROOT = Path("/workspace/reports/field6417-2025-2026")
DEFAULT_WORK_DIR = Path("/tmp/biweekly-api-jobs")

class EmptyCalendarCatalog:
    """Used when no Smart plan and no local pack is available."""

    def __init__(self, payload: dict | None = None):
        self.payload = payload or {}

    def list_seasons(self, land_id: str) -> list[dict]:
        return []

    def periods(self, land_id: str, season_id: str) -> list[dict]:
        raise ReportError("SEASON_NOT_FOUND")

    def period(self, land_id: str, season_id: str, period_id: str) -> dict:
        raise ReportError("PERIOD_NOT_FOUND")

    def load_snapshot(self, relative: str) -> dict:
        raise ReportError("SNAPSHOT_NOT_FOUND")

    def v8_optical_metrics(self) -> dict | None:
        return None

    def v8_pdf(self):
        return None

    def v8_prior_period(self) -> dict | None:
        return None


class CompositeCatalog:
    """Prefer a Smart native plan when present; otherwise the local 6417 pack."""

    def __init__(self, local: LocalPackCatalog | None, smart: SmartPlantingCatalog | EmptyCalendarCatalog | None):
        self.local = local
        self.smart = smart or EmptyCalendarCatalog()

    def _smart_has_plan(self, land_id: str) -> bool:
        if isinstance(self.smart, SmartPlantingCatalog):
            return self.smart.plan_available and str(land_id) == self.smart.land_id
        return False

    def list_seasons(self, land_id: str) -> list[dict]:
        if self._smart_has_plan(land_id):
            return self.smart.list_seasons(land_id)
        if self.local is not None:
            try:
                return self.local.list_seasons(land_id)
            except ReportError as exc:
                if exc.code != "LAND_NOT_IN_LOCAL_CATALOG":
                    raise
        return self.smart.list_seasons(land_id)

    def periods(self, land_id: str, season_id: str) -> list[dict]:
        if self._smart_has_plan(land_id):
            return self.smart.periods(land_id, season_id)
        if self.local is not None:
            try:
                return self.local.periods(land_id, season_id)
            except ReportError as exc:
                if exc.code not in {"LAND_NOT_IN_LOCAL_CATALOG", "SEASON_NOT_FOUND"}:
                    raise
        return self.smart.periods(land_id, season_id)

    def period(self, land_id: str, season_id: str, period_id: str) -> dict:
        if self._smart_has_plan(land_id):
            return self.smart.period(land_id, season_id, period_id)
        if self.local is not None:
            try:
                return self.local.period(land_id, season_id, period_id)
            except ReportError as exc:
                if exc.code not in {"LAND_NOT_IN_LOCAL_CATALOG", "SEASON_NOT_FOUND", "PERIOD_NOT_FOUND"}:
                    raise
        return self.smart.period(land_id, season_id, period_id)

    def load_snapshot(self, relative: str) -> dict:
        if self.local is not None:
            return self.local.load_snapshot(relative)
        raise ReportError("SNAPSHOT_NOT_FOUND")

    def v8_optical_metrics(self) -> dict | None:
        return self.local.v8_optical_metrics() if self.local is not None else None

    def v8_pdf(self):
        return self.local.v8_pdf() if self.local is not None else None

    def v8_prior_period(self) -> dict | None:
        return self.local.v8_prior_period() if self.local is not None else None

    def native_plan(self, land_id: str, season_id: str) -> dict | None:
        if isinstance(self.smart, SmartPlantingCatalog):
            try:
                return self.smart.native_plan(land_id, season_id)
            except ReportError:
                return None
        return None


def _local_pack() -> LocalPackCatalog | None:
    pack_root = Path(settings.biweekly_pack_root or DEFAULT_PACK_ROOT)
    if not (pack_root / "manifest.json").is_file():
        return None
    try:
        return LocalPackCatalog(pack_root=pack_root)
    except ReportError:
        return None


def _default_pdf_renderer():
    def _render(facts, dest):
        from app.reports.biweekly.official_pdf import render_official_biweekly_pdf

        render_official_biweekly_pdf(facts, dest)

    return _render


def build_trigger_service(
    smart: SmartPlantingCatalog | None = None,
    *,
    pdf_renderer=None,
    use_official_pdf: bool = False,
) -> TriggerService:
    work_dir = Path(settings.biweekly_work_dir or DEFAULT_WORK_DIR)
    work_dir.mkdir(parents=True, exist_ok=True)
    renderer = pdf_renderer
    if renderer is None and use_official_pdf:
        renderer = _default_pdf_renderer()
    official_store = None
    object_storage = None
    expected_bucket = None
    try:
        from app.services.biweekly_official_store import build_official_store

        official_store = build_official_store()
    except Exception as exc:  # noqa: BLE001
        # Construction failure is not "no jobs"; TriggerService will surface
        # OfficialStoreUnavailable on the first store operation if we attach a
        # broken factory. Leaving the store unset only when we cannot import it.
        from app.core.logging import logger

        logger.warning(
            "biweekly_official_store_unavailable_at_build err_type=%s",
            type(exc).__name__,
        )
        official_store = None
    try:
        from agric_satellite_analysis_common.storage import get_storage

        object_storage = get_storage()
        expected_bucket = object_storage.bucket()
    except Exception as exc:  # noqa: BLE001
        from app.core.logging import logger

        logger.warning("biweekly_object_storage_unavailable_at_build err_type=%s", type(exc).__name__)
        object_storage = None
        expected_bucket = None
    return TriggerService(
        work_dir=work_dir,
        catalog=CompositeCatalog(_local_pack(), smart or EmptyCalendarCatalog()),
        auto_run=True,
        pdf_renderer=renderer,
        official_store=official_store,
        object_storage=object_storage,
        expected_oss_bucket=expected_bucket,
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
            planned = dict(planned)
            if planned.get("dispatch") == "plan_only_local_fixture":
                planned["dispatch"] = "satellite_batch" if dispatch.get("dispatched") else "dispatch_failed"
            result["planned_batch"] = planned
            payload = dict(out.get("payload") or {})
            if payload:
                payload["planned_batch"] = planned
                out["payload"] = payload
        out["dispatched_to_production"] = bool(dispatch.get("dispatched"))
        out["satellite_batch_job_ids"] = list(dispatch.get("job_ids") or [])
    elif "dispatched_to_production" not in result:
        result["dispatched_to_production"] = False
        out["dispatched_to_production"] = False
    out["result"] = result
    if planned:
        out["planned_batch"] = planned
    return out
