"""Internal result apply endpoint (D3) — mirror mq_result_writer upserts over HTTP."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.middleware.internal_auth import InternalAuth

router = APIRouter(prefix="/internal/results", tags=["internal-results"])


class ApplyRequest(BaseModel):
    """ResultMessage-like or domain inline payload.

    Same shapes accepted by ``openfarm_common.result_apply.apply_result_envelope``.
    """

    result: dict[str, Any] = Field(default_factory=dict)
    # Allow top-level fields as convenience (merged into result if result empty-ish)
    status: str | None = None
    payload: dict[str, Any] | None = None
    inline: dict[str, Any] | None = None
    oss_urls: dict[str, Any] | None = None
    extras: dict[str, Any] | None = None
    error: str | None = None
    kind: str | None = None


class ApplyResponse(BaseModel):
    ok: bool = True
    stats: dict[str, Any] = Field(default_factory=dict)


def _envelope_from_body(body: ApplyRequest) -> dict[str, Any]:
    env = dict(body.result or {})
    if body.status is not None:
        env.setdefault("status", body.status)
    if body.payload is not None:
        env.setdefault("payload", body.payload)
    if body.inline is not None:
        env.setdefault("inline", body.inline)
    if body.oss_urls is not None:
        env.setdefault("oss_urls", body.oss_urls)
    if body.extras is not None:
        env.setdefault("extras", body.extras)
    if body.error is not None:
        env.setdefault("error", body.error)
    if body.kind is not None:
        env.setdefault("kind", body.kind)
        # Promote known domain fields from result root when kind set at top
        if body.kind and "kind" not in (body.result or {}):
            # If caller put domain fields only on body.result already, fine;
            # otherwise kind alone at top means result may hold the rest.
            pass
    return env


@router.post("/apply", response_model=ApplyResponse)
async def apply_results(
    body: ApplyRequest,
    _: InternalAuth,
):
    """Apply assessment/season job updates + weather/soil/scene upserts.

    Prefer ``POST /v1/internal/work/{id}/complete`` when a work_item lease
    exists; use this endpoint for legacy/MQ-free result delivery without a
    claim lease (e.g. weather/soil bulk after Celery finishes).
    """
    from openfarm_common.result_apply import apply_result_envelope

    envelope = _envelope_from_body(body)
    stats = await asyncio.to_thread(apply_result_envelope, envelope)
    ok = "error" not in stats
    return ApplyResponse(ok=ok, stats=stats)
