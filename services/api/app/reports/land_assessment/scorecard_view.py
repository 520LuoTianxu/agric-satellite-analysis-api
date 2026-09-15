# -*- coding: utf-8 -*-
"""Slim public scorecard for API / MQ / UI (no numpy)."""

from __future__ import annotations

from typing import Any

# Same order as WEIGHTS in scoring.py (clockwise from top on the radar).
DIMENSION_KEYS = (
    "crop",
    "soil",
    "vigor",
    "weather",
    "wet_safety",
    "drought_safety",
)


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def scorecard_public_view(
    scorecard: dict[str, Any] | None,
    *,
    ai: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a compact 6-dimension scorecard for JSON clients.

    Accepts the full scoring payload or an already-slim copy. Returns None
    when overall score or any of the six dimensions is missing so the UI
    never invents a partial hexagon.

    When ``ai`` carries a valid ``ai_reference_score``, attach an
    ``ai_reference`` block (reference only — never overwrites program overall).
    """
    if not isinstance(scorecard, dict):
        return None
    overall_in = scorecard.get("overall")
    if not isinstance(overall_in, dict):
        overall_in = {}
    overall_score = _as_float(overall_in.get("score"))
    if overall_score is None:
        return None

    by_key: dict[str, dict[str, Any]] = {}
    raw_dims = scorecard.get("dimensions")
    if isinstance(raw_dims, list):
        for item in raw_dims:
            if isinstance(item, dict) and item.get("key"):
                by_key[str(item["key"])] = item

    dimensions: list[dict[str, Any]] = []
    for key in DIMENSION_KEYS:
        item = by_key.get(key)
        if not item:
            return None
        score = _as_float(item.get("score"))
        if score is None:
            return None
        dimensions.append(
            {
                "key": key,
                "score": round(score, 1),
                "light": item.get("light"),
                "weight": item.get("weight"),
            }
        )

    out: dict[str, Any] = {
        "overall": {
            "score": round(overall_score, 1),
            "grade": overall_in.get("grade"),
            "light": overall_in.get("light"),
            "one_liner": overall_in.get("one_liner"),
        },
        "dimensions": dimensions,
    }
    conf = scorecard.get("confidence")
    if isinstance(conf, dict):
        conf_score = _as_float(conf.get("score"))
        if conf_score is not None:
            out["confidence"] = {"score": round(conf_score, 1)}

    # Prefer explicit ai=; also accept already-slim scorecard with ai_reference.
    ref_src = ai if isinstance(ai, dict) else None
    if ref_src is None and isinstance(scorecard.get("ai_reference"), dict):
        ref_src = {
            "ai_reference_score": (scorecard.get("ai_reference") or {}).get("score"),
            "ai_reference_grade": (scorecard.get("ai_reference") or {}).get("grade"),
            "ai_reference_light": (scorecard.get("ai_reference") or {}).get("light"),
            "ai_reference_rationale": (scorecard.get("ai_reference") or {}).get(
                "rationale"
            ),
            "ai_reference_disclaimer": (scorecard.get("ai_reference") or {}).get(
                "disclaimer"
            ),
        }
    elif ref_src is None and scorecard.get("ai_reference_score") is not None:
        ref_src = scorecard

    if isinstance(ref_src, dict):
        ref_score = _as_float(ref_src.get("ai_reference_score"))
        if ref_score is not None:
            ref_score = max(0.0, min(100.0, round(ref_score, 1)))
            disclaimer = (
                ref_src.get("ai_reference_disclaimer")
                or "AI参考分 · 不可作为准入结论"
            )
            out["ai_reference"] = {
                "score": ref_score,
                "grade": ref_src.get("ai_reference_grade"),
                "light": ref_src.get("ai_reference_light"),
                "rationale": ref_src.get("ai_reference_rationale"),
                "disclaimer": disclaimer,
            }
    return out
