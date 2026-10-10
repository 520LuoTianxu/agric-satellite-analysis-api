"""全部地块收获进度报表：JSON（分页）与 Excel 导出。只读已落库结果，不现场计算、不入队。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.database import get_db
from app.core.harvest_progress import HARVEST_PROGRESS_METHOD_VERSION
from app.middleware.auth import OrgContext, require_roles
from app.services import harvest_report as hr
from app.services.harvest_report_xlsx import build_workbook

router = APIRouter(prefix="/agri", tags=["agri"])
_reader = require_roles("owner", "admin", "member", "viewer")

MAX_RANGE_DAYS = 400
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class HarvestReportOut(BaseModel):
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int
    summary: dict[str, Any]
    facets: dict[str, Any]
    filters: dict[str, Any]
    method_version: str


def _filters(
    date_from: date | None = Query(
        None, alias="from", description="默认今天往前 60 天"
    ),
    date_to: date | None = Query(None, alias="to", description="默认今天"),
    group_id: str | None = Query(None),
    crop: str | None = Query(None, description="作物（land_parcels.crop_type）"),
    status_: str | None = Query(
        None,
        alias="status",
        description="进度分档，逗号分隔：none,lt30,30_90,ge90,not_computed",
    ),
    min_pct: float | None = Query(None, ge=0, le=100, description="最新合计%下限"),
    q: str | None = Query(None, description="地块ID或名称关键字"),
) -> hr.ReportFilters:
    today = date.today()
    d_to = date_to or today
    d_from = date_from or (d_to - timedelta(days=60))
    if d_from > d_to:
        raise HTTPException(422, "from 不能晚于 to")
    if (d_to - d_from).days > MAX_RANGE_DAYS:
        raise HTTPException(422, f"日期区间不能超过 {MAX_RANGE_DAYS} 天")
    buckets = [b.strip() for b in (status_ or "").split(",") if b.strip()]
    bad = [b for b in buckets if b not in hr.BUCKETS]
    if bad:
        raise HTTPException(
            422,
            f"未知进度分档 {','.join(bad)}；可选 {','.join(hr.BUCKETS)}",
        )
    return hr.ReportFilters(
        date_from=d_from,
        date_to=d_to,
        group_id=group_id or None,
        crop=crop or None,
        buckets=buckets,
        min_pct=min_pct,
        keyword=q or None,
    )


def _jsonable(p: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in p.items()}


def _filters_out(f: hr.ReportFilters) -> dict[str, Any]:
    return {
        "from": f.date_from.isoformat(),
        "to": f.date_to.isoformat(),
        "group_id": f.group_id,
        "crop": f.crop,
        "status": f.buckets,
        "min_pct": f.min_pct,
        "q": f.keyword,
    }


@router.get("/harvest-report", response_model=HarvestReportOut)
async def get_harvest_report(
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    filters: Annotated[hr.ReportFilters, Depends(_filters)],
    sort: str = Query("combined_pct", description="排序字段"),
    order: Literal["asc", "desc"] = Query("desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
) -> HarvestReportOut:
    parcels, _daily = await hr.load_report(db, filters)
    rows = hr.sort_parcels(hr.apply_filters(parcels, filters), sort, order)
    start = (page - 1) * page_size
    return HarvestReportOut(
        items=[_jsonable(p) for p in rows[start : start + page_size]],
        total=len(rows),
        page=page,
        page_size=page_size,
        summary=hr.summarize(rows),
        facets=hr.facets(parcels),
        filters=_filters_out(filters),
        method_version=HARVEST_PROGRESS_METHOD_VERSION,
    )


@router.get("/harvest-report/export.xlsx")
async def export_harvest_report(
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    filters: Annotated[hr.ReportFilters, Depends(_filters)],
    sort: str = Query("combined_pct"),
    order: Literal["asc", "desc"] = Query("desc"),
    carry_forward: bool = Query(True, description="透视表未入库日沿用上一期合计%"),
) -> Response:
    parcels, daily = await hr.load_report(db, filters)
    rows = hr.sort_parcels(hr.apply_filters(parcels, filters), sort, order)
    keep = {p["land_id"] for p in rows}
    content = await run_in_threadpool(
        build_workbook,
        rows,
        [r for r in daily if r["land_id"] in keep],
        hr.summarize(rows),
        filters,
        None,
        carry_forward,
    )
    name = f"harvest-report_{filters.date_from:%Y%m%d}-{filters.date_to:%Y%m%d}.xlsx"
    return Response(
        content=content,
        media_type=XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
