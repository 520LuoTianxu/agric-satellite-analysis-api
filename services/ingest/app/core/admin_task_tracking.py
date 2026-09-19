"""下载机 Celery 任务的管理员运行记录跟踪。"""

from __future__ import annotations

import json
import os
from functools import wraps
from typing import Any, Callable

import structlog

logger = structlog.get_logger()

ExecutionKeyBuilder = Callable[[tuple[Any, ...], dict[str, Any]], str]
ParamsBuilder = Callable[[tuple[Any, ...], dict[str, Any]], dict[str, Any]]


def _celery_task_id(args: tuple[Any, ...]) -> str | None:
    task = args[0] if args and hasattr(args[0], "request") else None
    value = getattr(getattr(task, "request", None), "id", None)
    return str(value) if value else None


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return {"repr": repr(value)}


def _is_retry_exception(exc: Exception) -> bool:
    # 不直接依赖 Celery 异常类，便于 ingest 单元测试在没有完整 Celery runtime
    # 时运行；Celery Retry 的稳定类名足以区分中间重试和最终失败。
    return exc.__class__.__name__ == "Retry"


def _report(
    run_id: str,
    status: str,
    *,
    celery_task_id: str | None,
    result: Any | None = None,
    error: str | None = None,
) -> None:
    try:
        from agric_satellite_analysis_common.internal_api import (
            update_admin_task_run_status,
        )

        update_admin_task_run_status(
            run_id,
            status,
            celery_task_id=celery_task_id,
            result=_json_safe(result) if result is not None else None,
            error=error,
            worker_name=os.getenv("WORKER_NAME") or "scheduled-task",
        )
    except Exception as exc:
        # 任务状态上报失败不能反向阻断遥感/统计主任务；管理页会保留最后一次状态。
        logger.warning(
            "admin_task_status_report_failed",
            run_id=run_id,
            status=status,
            error_type=type(exc).__name__,
        )


def track_admin_task_run(
    *,
    task_key: str,
    task_name: str,
    execution_key: ExecutionKeyBuilder,
    params: ParamsBuilder | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """自动任务创建并更新 AdminTaskRun；手动任务可通过 kwargs 传入已有 run_id。"""

    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(function)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            run_id = kwargs.get("admin_task_run_id")
            try:
                from agric_satellite_analysis_common.internal_api import (
                    ensure_admin_task_run,
                    internal_api_enabled,
                )

                if internal_api_enabled():
                    if not run_id:
                        run = ensure_admin_task_run(
                            task_key,
                            task_name,
                            execution_key(args, kwargs),
                            params=params(args, kwargs) if params else None,
                        )
                        run_id = str(run["run_id"])
                        # 将自动创建的记录传给任务函数，重试时沿用同一记录。
                        kwargs["admin_task_run_id"] = run_id
                    _report(
                        str(run_id),
                        "running",
                        celery_task_id=_celery_task_id(args),
                    )
            except Exception as exc:
                # 监控面是旁路能力；API 暂时不可达时仍允许主任务执行。
                logger.warning(
                    "admin_task_tracking_init_failed",
                    task_key=task_key,
                    error_type=type(exc).__name__,
                )

            try:
                result = function(*args, **kwargs)
            except Exception as exc:
                if run_id:
                    _report(
                        str(run_id),
                        "running" if _is_retry_exception(exc) else "failed",
                        celery_task_id=_celery_task_id(args),
                        error=None if _is_retry_exception(exc) else str(exc),
                    )
                raise
            if run_id:
                _report(
                    str(run_id),
                    "success",
                    celery_task_id=_celery_task_id(args),
                    result=result,
                )
            return result

        return wrapped

    return decorate


__all__ = ["track_admin_task_run"]
