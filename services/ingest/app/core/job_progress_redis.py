"""Ingest-facing Redis job progress helpers (flush to ``jobs.progress_json``)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm.attributes import flag_modified

from openfarm_common.job_progress_redis import (  # noqa: F401
    apply_redis_to_progress,
    clear_progress,
    incr_done,
    mark_scene_progress,
    merge_progress_for_api,
    progress_key,
    read_progress,
    reset_client_for_tests,
    set_total,
)


def flush_to_job(
    session,
    job,
    *,
    extra: dict[str, Any] | None = None,
    current_step: str | None = None,
    complete_process_scenes: bool = False,
) -> dict[str, Any]:
    """Write a compact Redis summary into ``job.progress_json`` and commit.

    Safe when Redis is down: still applies ``extra`` / ``current_step`` to the
    existing Postgres progress blob.
    """
    progress = dict(job.progress_json or {})
    snap = read_progress(getattr(job, "id", None))
    progress = apply_redis_to_progress(progress, snap)

    if current_step:
        progress["current_step"] = current_step

    if complete_process_scenes:
        steps = dict(progress.get("steps") or {})
        entry = dict(steps.get("process_scenes") or {})
        entry["status"] = "completed"
        steps["process_scenes"] = entry
        progress["steps"] = steps
        if progress.get("current_step") == "process_scenes":
            progress["current_step"] = current_step or "complete"

    if extra:
        # Shallow merge; nested ``steps`` / ``decloud`` should be passed whole.
        for k, v in extra.items():
            if k == "steps" and isinstance(v, dict):
                merged_steps = dict(progress.get("steps") or {})
                merged_steps.update(v)
                progress["steps"] = merged_steps
            else:
                progress[k] = v

    job.progress_json = progress
    flag_modified(job, "progress_json")
    session.commit()
    return progress
