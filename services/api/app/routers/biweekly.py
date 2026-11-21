"""Winter-wheat biweekly trigger routes on the API machine.

Mounted under /v1. Default disabled (BIWEEKLY_API_ENABLED=0).
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
from app.services.biweekly_dispatch import dispatch_bounded_backfill
from app.services.biweekly_mapping import load_wheat_phase_mapping
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
        service = build_trigger_service()
        payload = _public(service.list_seasons(land_id), principal)
        payload["phase_mapping"] = await load_wheat_phase_mapping()
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
        service = build_trigger_service()
        chosen = _season_for(service, land_id, season)
        payload = _public(service.list_periods(land_id, chosen), principal)
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
        service = build_trigger_service()
        chosen = _season_for(service, land_id, (body.season if body else None) or season)
        periods = service.catalog.periods(land_id, chosen)
        window = bounded_backfill_window(periods, period_id)
        # Local job record first so status is queryable even if dispatch is mocked.
        job = service.request_backfill(land_id, chosen, period_id)
        # Real linkage: API-machine create_satellite_batch_jobs + outbox/WorkItem.
        dispatch = await dispatch_bounded_backfill(land_id, window)
        # Explicitly do not call request_generate here.
        return _public(job, principal, dispatch=dispatch)
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
    """Generate only when explicitly requested. Reference plans are never implied."""
    try:
        service = build_trigger_service()
        payload = body or GenerateBody()
        chosen = _season_for(service, land_id, payload.season or season)
        period = service.catalog.period(land_id, chosen, period_id)
        observations = await load_scene_observations(land_id, period["start"], period["end_exclusive"])
        prior_obs = None
        previous = prior_period(service.catalog.periods(land_id, chosen), period_id)
        if previous is not None:
            prior_obs = await load_scene_observations(
                land_id, previous["start"], previous["end_exclusive"]
            )
        ref = payload.reference_planting_instance_id
        job = service.request_generate(
            land_id,
            chosen,
            period_id,
            reference_planting_instance_id=None if ref in (None, "") else str(ref),
            observations_override=observations,
            prior_observations_override=prior_obs,
            native_plan=payload.native_plan,
            observation_source=(
                "parcel_scene_products" if observations is not None else "pack_snapshot_or_empty"
            ),
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
        kind = job.get("kind")
        dispatch = job.get("result") if kind == "backfill" else None
        return _public(job, principal, dispatch=dispatch if kind == "backfill" else None)
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
