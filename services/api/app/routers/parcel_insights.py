"""地块对比、历史复盘与冻结报告；近期分析不会生成营销报告。"""

from datetime import date, datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from agric_satellite_analysis_common.phenology import infer_phenology
from app.core.database import get_db
from app.middleware.auth import OrgContext, org_scope, require_roles
from app.models.tables import Job, LandParcel
from app.schemas.parcel_insights import InsightsRequest
from app.services.parcel_insights import build_insights, load_points

router = APIRouter(tags=["parcel-insights"])
_reader = require_roles("owner", "admin", "member", "viewer")
_writer = require_roles("owner", "admin", "member")


@router.get("/lands/{land_id}/phenology")
async def phenology(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_reader)],
    db=Depends(get_db),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
):
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    end = end_date or today
    start = start_date or end - timedelta(days=550)
    if start > end or (end - start).days > 1100 or end > today:
        raise HTTPException(422, "请选择不超过 1100 天、且不晚于今天的推断区间")
    land = await db.get(LandParcel, land_id)
    if not land or land.deleted_at is not None:
        raise HTTPException(404, "地块不存在")
    points = (await load_points(db, [land_id], start, end))[land_id]
    return {
        "land_id": land_id,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        **infer_phenology(points, start=start, end=end),
    }


@router.post("/parcel-insights")
async def analyze(
    body: InsightsRequest,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db=Depends(get_db),
):
    result = await build_insights(db, body)
    now = datetime.now(timezone.utc)
    result.update(snapshot_id=None, created_at=now.isoformat())
    if body.mode == "historical":
        # 报告保存计算结果本身；下载时不重新读取影像、告警或天气，防止内容随时间漂移。
        snapshot_id = uuid4()
        result["snapshot_id"] = str(snapshot_id)
        db.add(
            Job(
                id=snapshot_id,
                land_id=body.land_ids[0],
                type="parcel_insights",
                status="succeeded",
                started_at=now,
                finished_at=now,
                params_json={"kind": "parcel_insights", "snapshot": result},
            )
        )
        await db.commit()
    return result


@router.get("/parcel-insights")
async def history(
    ctx: Annotated[OrgContext, Depends(_reader)],
    db=Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    predicate = (
        Job.type == "parcel_insights",
        Job.status == "succeeded",
        org_scope(None, ctx),
    )
    total = (
        await db.execute(select(func.count()).select_from(Job).where(*predicate))
    ).scalar_one()
    rows = (
        await db.execute(
            select(
                Job.id,
                Job.created_at,
                Job.params_json["snapshot"]["request"].label("request"),
            )
            .where(*predicate)
            .order_by(Job.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return {
        "items": [
            {"id": str(row.id), "created_at": row.created_at, "request": row.request}
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def read_snapshot(db, snapshot_id: UUID, ctx: OrgContext) -> dict:
    job = (
        await db.execute(
            select(Job).where(
                Job.id == snapshot_id,
                Job.type == "parcel_insights",
                Job.status == "succeeded",
                org_scope(None, ctx),
            )
        )
    ).scalar_one_or_none()
    snapshot = (job.params_json or {}).get("snapshot") if job else None
    if not snapshot or snapshot.get("request", {}).get("mode") != "historical":
        raise HTTPException(404, "历史分析快照不存在")
    return snapshot


@router.get("/parcel-insights/{snapshot_id}")
async def detail(
    snapshot_id: UUID, ctx: Annotated[OrgContext, Depends(_reader)], db=Depends(get_db)
):
    return await read_snapshot(db, snapshot_id, ctx)


@router.get("/parcel-insights/{snapshot_id}/report.pdf")
async def report(
    snapshot_id: UUID, ctx: Annotated[OrgContext, Depends(_reader)], db=Depends(get_db)
):
    from app.reports.parcel_insights import render_report

    snapshot = await read_snapshot(db, snapshot_id, ctx)
    pdf = await run_in_threadpool(render_report, snapshot)
    return Response(
        pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="parcel-insights-{snapshot_id}.pdf"',
            "Cache-Control": "private, no-store",
        },
    )
