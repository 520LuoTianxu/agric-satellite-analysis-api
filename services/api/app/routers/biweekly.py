"""Winter-wheat biweekly trigger routes on the API machine.

Mounted under /v1. Enabled by default (BIWEEKLY_API_ENABLED=1); set 0/false to disable.
Auth is the dedicated biweekly dependency — not AUTH_DISABLED.

Backfill creates bounded satellite_batch jobs through
create_satellite_batch_jobs / the existing outbox or WorkItem path.
Generate reads ingested parcel_scene_products and does not run automatically
after backfill.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from agric_satellite_analysis_common.biweekly.calendar import bounded_backfill_window, prior_period
from agric_satellite_analysis_common.biweekly.models import ReportError

from app.middleware.biweekly_auth import BiweeklyPrincipal, require_biweekly_access
from app.services.biweekly_dispatch import dispatch_bounded_backfill, dispatch_season_bounded_backfill
from app.services.biweekly_report_flow import (
    maybe_finalize_biweekly_job,
    start_period_generate,
    start_season_generate,
)
from app.services.biweekly_mapping import load_wheat_phase_mapping
from app.services.biweekly_plans import SmartPlantingCatalog, attach_public_stages, load_smart_planting_calendar
from app.services.biweekly_scenes import load_scene_observations
from app.services.biweekly_trigger import annotate_dispatch, build_trigger_service

router = APIRouter(prefix="/lands/{land_id}/biweekly", tags=["biweekly"])


def _content_disposition(filename: str) -> str:
    safe = (filename or "download.bin").replace('"', "").replace("\r", "").replace("\n", "")
    ascii_name = safe.encode("ascii", "ignore").decode("ascii").strip() or "download.bin"
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(safe)}'


HTTP_ERRORS = {
    "LOCAL_API_DISABLED": 503,
    "DEV_TOKEN_REQUIRED": 503,
    "ACCESS_DENIED": 403,
    "LAND_NOT_IN_LOCAL_CATALOG": 404,
    "SEASON_NOT_FOUND": 404,
    "PERIOD_NOT_FOUND": 404,
    "JOB_NOT_FOUND": 404,
    "UNKNOWN_REFERENCE": 422,
    "REFERENCE_FIXTURE_MISSING": 422,
    "BACKFILL_WINDOW_UNBOUNDED": 422,
    "ARTIFACT_UNAVAILABLE": 409,
    "ARTIFACT_CORRUPT": 409,
    "CATALOG_UNAVAILABLE": 503,
    "NOT_FOUND": 404,
    "BODY_INVALID": 400,
    "BODY_TOO_LARGE": 400,
    "CUTOFF_TIMEZONE_INVALID": 422,
    "JOB_KIND_UNSUPPORTED": 400,
    "UNSUPPORTED_CROP": 422,
    "LAND_ID_INVALID": 422,
    "PLAN_NOT_FOUND": 404,
    "SCENE_QUERY_FAILED": 503,
    "BACKFILL_FAILED": 503,
    "GENERATE_FAILED": 500,
    "FONT_MISSING": 503,
    "PDF_RENDERER_UNAVAILABLE": 503,
}


class GenerateBody(BaseModel):
    season: str | None = None
    reference_planting_instance_id: str | int | None = Field(
        default=None,
        description="Must be supplied by the caller; never implied from a default plan.",
    )
    native_plan: dict | None = None


class BackfillBody(BaseModel):
    season: str | None = None


def _error_payload(code: str) -> dict:
    return {"error": code, "production_ready": False}


def _http_for(exc: ReportError) -> JSONResponse:
    return JSONResponse(status_code=HTTP_ERRORS.get(exc.code, 400), content=_error_payload(exc.code))


def _public(payload: dict, principal: BiweeklyPrincipal, *, dispatch: dict | None = None) -> dict:
    out = annotate_dispatch(payload, dispatch)
    out["production_ready"] = False
    out["auto_publish"] = False
    out["auth"] = principal.public_auth()
    if dispatch is not None:
        out["dispatched_to_production"] = bool(dispatch.get("dispatched"))
        out["dispatch"] = dispatch
    return out



async def _smart_context(land_id: str) -> tuple[object, dict, dict]:
    mapping = await load_wheat_phase_mapping()
    calendar = await load_smart_planting_calendar(land_id, mapping_rows=mapping.get("rows"))
    service = build_trigger_service(SmartPlantingCatalog(calendar))
    return service, mapping, calendar


def _with_plan(payload: dict, mapping: dict, calendar: dict) -> dict:
    payload["phase_mapping"] = mapping
    payload["plan_available"] = bool(calendar.get("plan_available"))
    payload["plan_status"] = calendar.get("plan_status")
    payload["plan_source"] = calendar.get("plan_source")
    if calendar.get("note") and not payload.get("seasons"):
        payload["note"] = calendar["note"]
    elif calendar.get("note") and "note" not in payload:
        payload["note"] = calendar["note"]
    return attach_public_stages(payload, calendar)


def _season_for(service, land_id: str, season: str | None) -> str:
    if season:
        return season
    seasons = service.list_seasons(land_id)["seasons"]
    if len(seasons) == 1:
        return seasons[0]["season_id"]
    raise ReportError("SEASON_NOT_FOUND")


@router.get("/seasons")
async def list_seasons(
    land_id: str,
    principal: Annotated[BiweeklyPrincipal, Depends(require_biweekly_access)],
):
    try:
        service, mapping, calendar = await _smart_context(land_id)
        payload = _with_plan(_public(service.list_seasons(land_id), principal), mapping, calendar)
        return payload
    except ReportError as exc:
        return _http_for(exc)


@router.get("/periods")
async def list_periods(
    land_id: str,
    principal: Annotated[BiweeklyPrincipal, Depends(require_biweekly_access)],
    season: Annotated[str | None, Query()] = None,
):
    try:
        service, mapping, calendar = await _smart_context(land_id)
        chosen = _season_for(service, land_id, season)
        payload = _with_plan(_public(service.list_periods(land_id, chosen), principal), mapping, calendar)
        # Enrich empty calendar periods with ingested scene counts when available.
        for row in payload.get("periods") or []:
            counts = await _scene_counts(land_id, row["start"], row["end_exclusive"])
            if counts:
                row.update(counts)
        return payload
    except ReportError as exc:
        return _http_for(exc)


async def _scene_counts(land_id: str, start: str, end_exclusive: str) -> dict | None:
    rows = await load_scene_observations(land_id, start, end_exclusive)
    if rows is None:
        return None
    optical = [row for row in rows if row.get("quality_status") == "eligible_descriptive"]
    radar = [row for row in rows if str(row.get("sensor") or "").upper() == "S1"]
    return {
        "optical_descriptive_days": len({row["date"] for row in optical}),
        "s1_products": len(radar),
        "has_eligible_optics": bool(optical),
        "observation_source": "parcel_scene_products",
    }


@router.post("/periods/{period_id}/backfill")
async def request_backfill(
    land_id: str,
    period_id: str,
    principal: Annotated[BiweeklyPrincipal, Depends(require_biweekly_access)],
    body: BackfillBody | None = None,
    season: Annotated[str | None, Query()] = None,
):
    """Create a bounded satellite_batch plan and dispatch it. Never auto-generates."""
    try:
        service, _mapping, _calendar = await _smart_context(land_id)
        chosen = _season_for(service, land_id, (body.season if body else None) or season)
        periods = service.catalog.periods(land_id, chosen)
        window = bounded_backfill_window(periods, period_id)
        # Queue a job record, but do not evaluate a local pack snapshot.
        # Smart periods have snapshot=None; running the fixture would false-fail.
        job = service.request_backfill(
            land_id, chosen, period_id, evaluate_local_fixture=False
        )
        try:
            dispatch = await dispatch_bounded_backfill(land_id, window)
        except ReportError as exc:
            service.record_backfill_dispatch(
                job["job_id"],
                {"dispatched": False, "reason": exc.code, "job_ids": []},
            )
            raise
        job = service.record_backfill_dispatch(job["job_id"], dispatch)
        # Explicitly do not call request_generate here.
        return _public(job, principal, dispatch=dispatch)
    except ReportError as exc:
        return _http_for(exc)


@router.post("/backfill-season")
async def request_season_backfill(
    land_id: str,
    principal: Annotated[BiweeklyPrincipal, Depends(require_biweekly_access)],
    body: BackfillBody | None = None,
    season: Annotated[str | None, Query()] = None,
):
    """Dispatch bounded satellite_batch jobs for every period in the season.

    Each period uses its own window (period plus prior comparison). Never
    sends one unbounded or three-year window, and never auto-generates.
    """
    try:
        service, _mapping, _calendar = await _smart_context(land_id)
        chosen = _season_for(service, land_id, (body.season if body else None) or season)
        periods = service.catalog.periods(land_id, chosen)
        job = await start_season_generate(
            service,
            land_id=land_id,
            season_id=chosen,
            reference_planting_instance_id=None,
        )
        return _public(job, principal)
    except ReportError as exc:
        return _http_for(exc)


@router.post("/periods/{period_id}/generate")
async def request_generate(
    land_id: str,
    period_id: str,
    principal: Annotated[BiweeklyPrincipal, Depends(require_biweekly_access)],
    body: GenerateBody | None = None,
    season: Annotated[str | None, Query()] = None,
):
    """Backfill the period first, then render an official PDF. Reference is never implied."""
    try:
        service, mapping, calendar = await _smart_context(land_id)
        payload = body or GenerateBody()
        chosen = _season_for(service, land_id, payload.season or season)
        ref = payload.reference_planting_instance_id
        native = payload.native_plan
        if native is None and hasattr(service.catalog, "native_plan"):
            native = service.catalog.native_plan(land_id, chosen)
        job = await start_period_generate(
            service,
            land_id=land_id,
            season_id=chosen,
            period_id=period_id,
            reference_planting_instance_id=None if ref in (None, "") else str(ref),
            native_plan=native,
        )
        return _public(job, principal)
    except ReportError as exc:
        return _http_for(exc)


@router.get("/jobs/{job_id}")
async def get_job(
    land_id: str,
    job_id: str,
    principal: Annotated[BiweeklyPrincipal, Depends(require_biweekly_access)],
):
    try:
        service = build_trigger_service()
        job = service.get_job(job_id)
        if str(job.get("land_id")) != str(land_id):
            raise ReportError("JOB_NOT_FOUND")
        if job.get("status") == "dispatched":
            job = (await maybe_finalize_biweekly_job(job_id, service=service)) or job
        kind = job.get("kind")
        dispatch = job.get("result") if kind in {"backfill", "backfill_season"} else None
        return _public(job, principal, dispatch=dispatch if kind in {"backfill", "backfill_season"} else None)
    except ReportError as exc:
        return _http_for(exc)


@router.get("/jobs/{job_id}/download")
async def download_job(
    land_id: str,
    job_id: str,
    principal: Annotated[BiweeklyPrincipal, Depends(require_biweekly_access)],
):
    try:
        service = build_trigger_service()
        job = service.get_job(job_id)
        if str(job.get("land_id")) != str(land_id):
            raise ReportError("JOB_NOT_FOUND")
        filename, data, mime = service.download(job_id)
        return Response(
            content=data,
            media_type=mime or "application/octet-stream",
            headers={"Content-Disposition": _content_disposition(filename)},
        )
    except ReportError as exc:
        return _http_for(exc)
