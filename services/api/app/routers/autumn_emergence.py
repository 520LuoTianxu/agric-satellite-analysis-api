"""冬小麦秋季出苗接口。

GET /v1/agri/lands/{land_id}/autumn-emergence
GET /v1/agri/lands/{land_id}/autumn-emergence/pixels
POST /v1/agri/lands/{land_id}/autumn-emergence

像元来自 emergence/autumn/ 对象，不读 s1s2_parcel/json 的 NDVI 列。
POST 与更新影像一样创建 satellite_batch，由下载机领取。API 本机不下载影像。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.auth import OrgContext, require_roles
from app.schemas.autumn_emergence import (
    AutumnEmergenceOut,
    AutumnEmergencePixelsOut,
    AutumnEmergenceScheduleOut,
    AutumnEmergenceTriggerIn,
    EmergencePixel,
    class_labels,
)
from app.services import autumn_emergence as emergence

router = APIRouter(prefix="/agri", tags=["autumn-emergence"])

_reader = require_roles("owner", "admin", "member", "viewer")
_writer = require_roles("owner", "admin", "member")
_SEASON = re.compile(r"^\d{4}-autumn$")


async def _agri_ready(db: AsyncSession) -> None:
    found = await db.execute(
        text(
            "SELECT 1 FROM information_schema.schemata "
            "WHERE schema_name = 'agric_satellite' LIMIT 1"
        )
    )
    if found.scalar() is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "agric_satellite schema not installed; "
                "run scripts/20261126_parcel_autumn_emergence.sql"
            ),
        )
    ready = await db.execute(
        text(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'agric_satellite'
              AND table_name = 'parcel_autumn_emergence_requests'
            LIMIT 1
            """
        )
    )
    if ready.scalar() is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "autumn emergence manual trigger is not installed; "
                "run scripts/20261126_parcel_autumn_emergence_manual.sql"
            ),
        )


def _season_or_422(season: str | None) -> str | None:
    if season is None or season == "":
        return None
    if not _SEASON.fullmatch(season):
        raise HTTPException(
            status_code=422, detail="season 须为 YYYY-autumn，例如 2026-autumn"
        )
    return season


@router.get(
    "/lands/{land_id}/autumn-emergence",
    response_model=AutumnEmergenceOut,
)
async def get_autumn_emergence(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    season: str | None = Query(
        None, description="播种年秋季，例如 2026-autumn。缺省为当前或最近一季。"
    ),
):
    """地块本季出苗摘要：状态、冻结标记、本底、噪声和逐日中位数。

    人工拉取的进度在 job_status、job_ids、claimed_by、failure_reason
    和分片计数上，与更新影像看 GET /v1/jobs/{job_id} 相同。
    已出苗后 emerged_flag 与 frozen 为 true，同季不再重算。
    """
    await _agri_ready(db)
    view = await emergence.get_summary(db, land_id, _season_or_422(season))
    if view is None:
        raise HTTPException(status_code=404, detail="Land parcel not found")
    return AutumnEmergenceOut.model_validate(view)


@router.post(
    "/lands/{land_id}/autumn-emergence",
    response_model=AutumnEmergenceScheduleOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_autumn_emergence(
    land_id: str,
    body: AutumnEmergenceTriggerIn,
    ctx: Annotated[OrgContext, Depends(_writer)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """按播种日派发出苗拉取，逻辑与更新影像的 backfill-indices 相同。

    始终创建 satellite_batch（展示名「出苗」，date_from 为播种日，只有 S2）。
    下载机领取；已入库的晴空景由该任务跳过而不重下。官方 S2 回执入库后，
    或该任务完成回执在同一事务写入计算 outbox 后，API 再算出苗。
    完成回执若丢失，GET 在任务已完成且尚无结果时补入计算队列。
    本接口不下载影像，也不修改 crop_type。
    进度查询返回的 job_ids：GET /v1/jobs/{job_id}。已冻结的季不重新派发。
    """
    await _agri_ready(db)
    try:
        scheduled = await emergence.schedule_manual_emergence(
            db,
            land_id,
            body.sowing_date,
            _season_or_422(body.season),
        )
    except emergence.EmergenceScheduleError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if scheduled is None:
        raise HTTPException(status_code=404, detail="Land parcel not found")
    await db.commit()
    return AutumnEmergenceScheduleOut.model_validate(scheduled)


@router.get(
    "/lands/{land_id}/autumn-emergence/pixels",
    response_model=AutumnEmergencePixelsOut,
)
async def get_autumn_emergence_pixels(
    land_id: str,
    ctx: Annotated[OrgContext, Depends(_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    season: str | None = Query(None, description="播种年秋季，例如 2026-autumn"),
    on: date | None = Query(
        None,
        alias="date",
        description="晴空产品日。缺省为冻结或最近一次计算的 as_of_date。",
    ),
):
    """逐像元增量与类别，点集为 lon/lat，热力图按 increment 上色、按 class 分类。

    0 未明显高于本底，1 高出噪声但尚未持续，2 持续高于本底噪声。
    """
    await _agri_ready(db)
    view = await emergence.get_pixels(db, land_id, _season_or_422(season), on)
    if view is None:
        raise HTTPException(status_code=404, detail="Land parcel not found")
    if not view.get("eligible"):
        raise HTTPException(
            status_code=404, detail="该地块不是冬小麦，没有秋季出苗图层"
        )
    if view.get("missing"):
        raise HTTPException(status_code=404, detail="秋季出苗像元尚未计算")
    if view.get("missing_date"):
        as_of = view.get("as_of_date")
        raise HTTPException(
            status_code=404,
            detail=f"该日期没有出苗像元快照，当前快照日期为 {as_of}",
        )
    return AutumnEmergencePixelsOut(
        land_id=view["land_id"],
        field_id=view["field_id"],
        season_key=view["season_key"],
        date=view["date"],
        as_of_date=view.get("as_of_date"),
        method_version=view["method_version"],
        noise_floor=view.get("noise_floor"),
        frozen=bool(view.get("frozen")),
        emerged_flag=bool(view.get("emerged_flag")),
        status=view.get("status"),
        status_zh=view.get("status_zh"),
        class_labels=class_labels(),
        disclaimer_zh=view["disclaimer_zh"],
        pixel_count=view["pixel_count"],
        counts=view["counts"],
        pixels_lonlat=[
            EmergencePixel.model_validate(pixel) for pixel in view["pixels_lonlat"]
        ],
    )
