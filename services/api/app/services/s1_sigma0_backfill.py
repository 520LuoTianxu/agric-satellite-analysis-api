"""只对缺少 ESA Sigma0 定标的 S1 场景日期编排受控回算。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime, timezone
from typing import Any

from agric_satellite_analysis_common.task_priority import BACKGROUND_TASK_PRIORITY
from agric_satellite_analysis_common.trace import stamp_trace_on_payload
from sqlalchemy import select, text

from app.core.config import settings
from app.models.tables import (
    AdminTaskRun,
    Job,
    LandParcel,
    S1Sigma0DispatchOutbox,
    WorkItem,
)
from app.services.satellite_batch import build_satellite_batch_jobs, satellite_land_geometry
from app.services.satellite_history import default_history_window
from app.services.work_items import work_queue_mode

S1_SIGMA0_BACKFILL_MAX_LANDS = 100
S1_SIGMA0_BACKFILL_MAX_TARGET_DATES = 50_000
S1_SIGMA0_BACKFILL_MAX_JOBS = 500


async def run_s1_sigma0_calibration_backfill(
    *,
    land_ids: Sequence[str],
    years: int = 5,
    parent_job_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """仅重算所选地块中未使用 ESA 双极化 LUT 的 S1 产品日期。"""
    from app.core.database import async_session

    requested = list(dict.fromkeys(str(value).strip() for value in land_ids))
    if not requested or any(not value for value in requested):
        raise ValueError("S1定标回算必须提供非空地块编号")
    if len(requested) > S1_SIGMA0_BACKFILL_MAX_LANDS:
        raise ValueError(
            f"单次S1定标回算最多选择{S1_SIGMA0_BACKFILL_MAX_LANDS}个地块"
        )
    if years < 1 or years > 10:
        raise ValueError("S1定标回算年限必须在1到10年之间")

    date_from, date_to = default_history_window(years=years)
    execution_id = parent_job_id or uuid.uuid4()
    async with async_session() as db:
        # 多API实例也必须串行检查并登记回算任务，避免同地块日期被重复并发下载/覆盖。
        await db.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('s1-sigma0-calibration-backfill', 0))"
            )
        )
        admin_run = await db.get(AdminTaskRun, execution_id)
        if admin_run is not None and admin_run.status in {
            "success",
            "failed",
            "cancelled",
            "partial",
        }:
            # 多个API实例可能同时恢复同一运行记录；取得规划锁后再次核验终态，防止重复建Job。
            if isinstance(admin_run.result_json, dict):
                return admin_run.result_json
            return {
                "status": admin_run.status,
                "parent_job_id": str(execution_id),
                "message": "管理员运行记录已进入终态，无需重复派发",
            }
        active_overlap = await db.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM agric_satellite.jobs
                    WHERE type = 'satellite_batch'
                      AND status IN ('pending', 'running')
                      AND params_json->>'s1_sigma0_calibration_backfill' = 'true'
                      AND COALESCE(params_json->'land_ids', '[]'::jsonb)
                          ?| CAST(:land_ids AS text[])
                )
                """
            ),
            {"land_ids": requested},
        )
        if active_overlap.scalar_one():
            raise ValueError("所选地块已有S1 Sigma0回算任务排队或处理中，请完成后再触发")

        land_rows = list(
            (
                await db.execute(
                    select(LandParcel).where(
                        LandParcel.land_id.in_(requested),
                        LandParcel.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        land_by_id = {str(land.land_id): land for land in land_rows}
        missing_land_ids = [land_id for land_id in requested if land_id not in land_by_id]

        # 以像元内真实的定标方法识别旧产品；不能只看算法标签后直接覆盖有效Sigma0结果。
        rows = (
            await db.execute(
                text(
                    """
                    SELECT land_id, date
                    FROM agric_satellite.parcel_scene_products
                    WHERE sensor = 'S1'
                      -- 保留索引列原样参与等值匹配，支持地块/传感器/日期复合索引。
                      AND land_id = ANY(CAST(:land_ids AS text[]))
                      AND date BETWEEN :date_from AND :date_to
                      AND LOWER(COALESCE(
                            NULLIF(BTRIM(pixel_data->'radiometric_calibration'->>'method'), ''),
                            ''
                          )) <> 'esa_sigma_nought_lut'
                    ORDER BY land_id, date
                    """
                ),
                {
                    "land_ids": requested,
                    "date_from": date_from,
                    "date_to": date_to,
                },
            )
        ).mappings().all()

        targets: dict[str, set[date]] = {}
        for row in rows:
            land_id = str(row["land_id"])
            scene_date = row["date"]
            if land_id not in land_by_id or not isinstance(scene_date, date):
                continue
            targets.setdefault(land_id, set()).add(scene_date)
        target_date_count = sum(len(days) for days in targets.values())
        if target_date_count > S1_SIGMA0_BACKFILL_MAX_TARGET_DATES:
            raise ValueError(
                "本次目标日期超过5万条安全上限，请缩短年限或拆分地块清单"
            )

        valid_lands: list[LandParcel] = []
        skipped_geometry_land_ids: list[str] = []
        for land_id, scene_dates in targets.items():
            land = land_by_id[land_id]
            try:
                satellite_land_geometry(land)
            except ValueError:
                skipped_geometry_land_ids.append(land_id)
                continue
            if scene_dates:
                valid_lands.append(land)

        target_dates_by_land = {
            land_id: sorted(scene_date.isoformat() for scene_date in scene_dates)
            for land_id, scene_dates in targets.items()
            if land_id not in skipped_geometry_land_ids
        }
        selected_date_count = sum(len(days) for days in target_dates_by_land.values())
        if not selected_date_count:
            return {
                "status": "completed",
                "parent_job_id": str(execution_id),
                "requested_land_count": len(requested),
                "land_count": 0,
                "target_date_count": 0,
                "missing_land_ids": missing_land_ids,
                "skipped_geometry_land_ids": skipped_geometry_land_ids,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "message": "所选范围内没有缺少ESA Sigma0定标的S1场景",
            }

        per_land_windows = {
            land_id: (
                min(date.fromisoformat(value) for value in days),
                max(date.fromisoformat(value) for value in days),
            )
            for land_id, days in target_dates_by_land.items()
        }
        target_from = min(window[0] for window in per_land_windows.values())
        target_to = max(window[1] for window in per_land_windows.values())
        groups, jobs = build_satellite_batch_jobs(
            valid_lands,
            date_from=target_from,
            date_to=target_to,
            sensors=("S1",),
            force=True,
            parent_job_id=execution_id,
            id_namespace=execution_id,
            chunk_days=settings.index_backfill_chunk_days,
            land_date_windows=per_land_windows,
            # 此任务可被管理员手动反复触发，单次子任务数再收紧于全局通用上限。
            max_jobs=S1_SIGMA0_BACKFILL_MAX_JOBS,
            extra_params={
                "target_dates_by_land": target_dates_by_land,
                "require_sigma0": True,
                "s1_sigma0_calibration_backfill": True,
            },
        )
        if not jobs:
            return {
                "status": "completed",
                "parent_job_id": str(execution_id),
                "requested_land_count": len(requested),
                "land_count": len(valid_lands),
                "target_date_count": selected_date_count,
                "missing_land_ids": missing_land_ids,
                "skipped_geometry_land_ids": skipped_geometry_land_ids,
                "date_from": target_from.isoformat(),
                "date_to": target_to.isoformat(),
                "message": "目标日期未生成可执行的空间分片任务",
            }

        parent = Job(
            id=execution_id,
            land_id=jobs[0].land_id,
            type="s1_sigma0_calibration_backfill",
            status="pending",
            progress_json={
                "stage": "queued",
                "land_count": len(valid_lands),
                "group_count": len(groups),
                "job_count": len(jobs),
                "target_date_count": selected_date_count,
            },
            params_json={
                "land_ids": [str(land.land_id) for land in valid_lands],
                "requested_land_count": len(requested),
                "missing_land_ids": missing_land_ids,
                "skipped_geometry_land_ids": skipped_geometry_land_ids,
                "years": years,
                "date_from": target_from.isoformat(),
                "date_to": target_to.isoformat(),
                "target_date_count": selected_date_count,
                "sensors": ["S1"],
                "force": True,
                "require_sigma0": True,
                "job_ids": [str(job.id) for job in jobs],
            },
        )
        db.add(parent)
        await db.flush()
        db.add_all(jobs)

        queue_mode = work_queue_mode()
        if queue_mode == "claim":
            # 生产/测试claim模式以WorkItem为持久队列；与Job同事务提交，API退出也不会丢派发。
            work_items = []
            for job in jobs:
                job_id = str(job.id)
                extras = {
                    "job_id": job_id,
                    "priority": BACKGROUND_TASK_PRIORITY,
                }
                payload = stamp_trace_on_payload(
                    {
                        "land_id": str(job.land_id),
                        "extras": extras,
                        "task_id": job_id,
                    }
                )
                work_items.append(
                    WorkItem(
                        type="satellite_batch",
                        parent_job_id=job.id,
                        payload_json=payload,
                        status="pending",
                        priority=BACKGROUND_TASK_PRIORITY,
                        attempts=0,
                        idempotency_key=f"satellite_batch:{job_id}",
                    )
                )
            db.add_all(work_items)

            claim_result = {
                "status": "queued",
                "parent_job_id": str(execution_id),
                "requested_land_count": len(requested),
                "land_count": len(valid_lands),
                "group_count": len(groups),
                "target_date_count": selected_date_count,
                "job_count": len(jobs),
                "queued_job_ids": [str(job.id) for job in jobs],
                "failed_job_ids": [],
                "missing_land_ids": missing_land_ids,
                "skipped_geometry_land_ids": skipped_geometry_land_ids,
                "date_from": target_from.isoformat(),
                "date_to": target_to.isoformat(),
            }
            parent.status = "running"
            parent.progress_json = {
                **(parent.progress_json or {}),
                "stage": "queued",
                "queued_count": len(jobs),
                "failed_count": 0,
            }

            # 管理员运行记录与队列事务原子落库，避免回算已入队但页面状态永久停留运行中。
            admin_run = await db.get(AdminTaskRun, execution_id)
            if admin_run is not None:
                now = datetime.now(timezone.utc)
                admin_run.status = "success"
                admin_run.result_json = claim_result
                admin_run.finished_at = now
                admin_run.updated_at = now
            await db.commit()
            return claim_result

        # legacy/dual 模式将子任务和派发意图同事务提交；API进程崩溃后由Outbox租约扫描恢复。
        for job in jobs:
            progress = dict(job.progress_json or {})
            progress.update(
                {"dispatch_status": "pending", "dispatch_attempts": 0}
            )
            job.progress_json = progress
        db.add_all(
            [
                S1Sigma0DispatchOutbox(
                    job_id=job.id,
                    land_id=str(job.land_id),
                    status="pending",
                    attempts=0,
                )
                for job in jobs
            ]
        )
        queued_job_ids = [str(job.id) for job in jobs]
        parent.status = "running"
        parent.progress_json = {
            **(parent.progress_json or {}),
            "stage": "dispatch_pending",
            "queued_count": len(jobs),
            "failed_count": 0,
        }
        # 根任务、子任务和每个派发意图一次提交，避免出现只有其中一部分持久化的状态。
        await db.commit()

    return {
        "status": "queued",
        "parent_job_id": str(execution_id),
        "requested_land_count": len(requested),
        "land_count": len(valid_lands),
        "group_count": len(groups),
        "target_date_count": selected_date_count,
        "job_count": len(jobs),
        "queued_job_ids": queued_job_ids,
        "failed_job_ids": [],
        "missing_land_ids": missing_land_ids,
        "skipped_geometry_land_ids": skipped_geometry_land_ids,
        "date_from": target_from.isoformat(),
        "date_to": target_to.isoformat(),
    }


__all__ = [
    "S1_SIGMA0_BACKFILL_MAX_LANDS",
    "S1_SIGMA0_BACKFILL_MAX_TARGET_DATES",
    "S1_SIGMA0_BACKFILL_MAX_JOBS",
    "run_s1_sigma0_calibration_backfill",
]
