"""统一任务优先级定义及 Redis/Celery 优先级适配。"""

from __future__ import annotations

from typing import Any

from celery.signals import before_task_publish

# 业务层使用“数值越大越优先”，这样和 API work_items 的降序认领规则一致。
BACKGROUND_TASK_PRIORITY = 0
MANUAL_TASK_PRIORITY = 5
INTERACTIVE_REPORT_PRIORITY = 9
TASK_PRIORITY_MIN = 0
TASK_PRIORITY_MAX = 9

# Kombu Redis transport 实际按数值越小越先出队，且 priority=0 在部分版本中
# 会被当作“未设置”。因此把业务优先级转换到 1..9，避免高优先级被默认值覆盖。
CELERY_BACKGROUND_PRIORITY = 9
CELERY_PRIORITY_STEPS = tuple(range(10))

_PRIORITY_SIGNALS_INSTALLED = False


def normalize_task_priority(
    value: Any,
    *,
    default: int = BACKGROUND_TASK_PRIORITY,
) -> int:
    """将外部或历史任务优先级限制在统一的 0..9 范围。"""
    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not a task priority")
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(TASK_PRIORITY_MIN, min(TASK_PRIORITY_MAX, parsed))


def celery_priority_for(task_priority: Any) -> int:
    """把业务优先级转换为 Redis transport 的出队优先级。"""
    logical = normalize_task_priority(task_priority)
    return max(1, TASK_PRIORITY_MAX - logical)


def inject_default_celery_priority(
    *,
    properties: dict[str, Any] | None = None,
    **_: Any,
) -> None:
    """让未显式标注的历史/定时 Celery 任务进入后台优先级队列。"""
    if properties is None or properties.get("priority") is not None:
        return
    properties["priority"] = CELERY_BACKGROUND_PRIORITY


def install_priority_signals() -> None:
    """在每个 Celery 进程中安装一次后台默认优先级钩子。"""
    global _PRIORITY_SIGNALS_INSTALLED
    if _PRIORITY_SIGNALS_INSTALLED:
        return
    before_task_publish.connect(
        inject_default_celery_priority,
        weak=False,
        dispatch_uid="agric_satellite_analysis_default_task_priority",
    )
    _PRIORITY_SIGNALS_INSTALLED = True


__all__ = [
    "BACKGROUND_TASK_PRIORITY",
    "CELERY_BACKGROUND_PRIORITY",
    "CELERY_PRIORITY_STEPS",
    "INTERACTIVE_REPORT_PRIORITY",
    "MANUAL_TASK_PRIORITY",
    "TASK_PRIORITY_MAX",
    "TASK_PRIORITY_MIN",
    "celery_priority_for",
    "inject_default_celery_priority",
    "install_priority_signals",
    "normalize_task_priority",
]
