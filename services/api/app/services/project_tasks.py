"""项目级下载机任务（项目 COG 补数、项目收获分析）的派发。

claim 模式下 Celery Redis 在下载机本地，API 直接 ``send_task`` 只会进 API 侧 Redis，
下载机永远收不到。因此与 ``admin_task`` 一样：claim 模式先写 ``work_items``
（type=``project_task``），由下载机 work_agent 领取后再投递本机队列；
legacy/dual 模式保持原来的直接 ``send_task``。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import WorkItem
from app.services import work_items as wi

PROJECT_TASK_TYPE = "project_task"
BUILD_PROJECT_COGS = "app.tasks.project_cogs.build_project_cogs"
RUN_PROJECT_HARVEST = "app.tasks.project_harvest.run_project_harvest"
# 下载机侧同样有一份白名单；两边都校验，避免 work_items 变成任意任务执行器。
PROJECT_TASK_NAMES = frozenset({BUILD_PROJECT_COGS, RUN_PROJECT_HARVEST})


def use_work_items() -> bool:
    return wi.work_queue_mode() == "claim"


async def enqueue_project_task(
    db: AsyncSession,
    *,
    task_name: str,
    kwargs: dict[str, Any],
    dedup_key: str,
    unique: bool = False,
) -> tuple[WorkItem, bool]:
    """写入 project_task 工作项，返回 (item, reused)。不提交事务。

    - ``unique=True``：``dedup_key`` 本身全局唯一（如 run_id），直接作为幂等键；
    - ``unique=False``：同一 ``dedup_key`` 若已有 pending/leased 项则复用，
      否则追加短随机后缀新建，允许任务完成后再次手动补数。
    """
    if task_name not in PROJECT_TASK_NAMES:
        raise ValueError(f"unsupported project task: {task_name}")
    base = f"{PROJECT_TASK_TYPE}:{dedup_key}"
    payload = {"task_name": task_name, "kwargs": dict(kwargs)}
    if unique:
        item = await wi.enqueue_work_item(
            db, type=PROJECT_TASK_TYPE, payload=payload, idempotency_key=base
        )
        return item, item.status != "pending" or item.attempts > 0
    existing = (
        await db.execute(
            select(WorkItem)
            .where(
                WorkItem.type == PROJECT_TASK_TYPE,
                WorkItem.status.in_(("pending", "leased")),
                WorkItem.idempotency_key.like(base.replace("%", r"\%").replace("_", r"\_") + "%"),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, True
    item = await wi.enqueue_work_item(
        db,
        type=PROJECT_TASK_TYPE,
        payload=payload,
        idempotency_key=f"{base}:{uuid.uuid4().hex[:12]}",
    )
    return item, False
