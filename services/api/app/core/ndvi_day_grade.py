"""Per-date NDVI day-grade shares from lonlat_v1 pixels (图一 bands).

Bands match the web chart ``computePixelNdviGradeShares``:
红<0.25 / 橙0.25–0.35 / 黄0.35–0.50 / 绿≥0.50.
When any pixel has clear=1, only clear pixels are counted.
"""

from __future__ import annotations

from typing import Any, Literal

NdviDayGrade = Literal["红", "橙", "黄", "绿"]

NDVI_DAY_GRADE_ORDER: tuple[NdviDayGrade, ...] = ("红", "橙", "黄", "绿")
NDVI_DAY_GRADE_RULE_ZH = "红<0.25 / 橙0.25–0.35 / 黄0.35–0.50 / 绿≥0.50"


def classify_ndvi_day_grade(v: float) -> NdviDayGrade:
    if v >= 0.5:
        return "绿"
    if v >= 0.35:
        return "黄"
    if v >= 0.25:
        return "橙"
    return "红"


def _pixel_ndvi(pix: dict[str, Any]) -> float | None:
    raw = pix.get("NDVI", pix.get("ndvi"))
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def compute_pixel_ndvi_day_grade_shares(
    pixels: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Aggregate pixel NDVI into day-grade counts/pct/mean.

    Returns None when no usable NDVI values remain after clear filtering.
    """
    if not pixels:
        return None
    any_clear = any(
        int(p.get("clear") or 0) == 1 for p in pixels if isinstance(p, dict)
    )
    counts: dict[str, int] = {g: 0 for g in NDVI_DAY_GRADE_ORDER}
    total = 0.0
    n = 0
    for p in pixels:
        if not isinstance(p, dict):
            continue
        if any_clear and int(p.get("clear") or 0) != 1:
            continue
        v = _pixel_ndvi(p)
        if v is None:
            continue
        counts[classify_ndvi_day_grade(v)] += 1
        total += v
        n += 1
    if not n:
        return None
    pct = {g: round(counts[g] * 1000 / n) / 10 for g in NDVI_DAY_GRADE_ORDER}
    return {
        "counts": counts,
        "pct": pct,
        "n": n,
        "mean": total / n,
    }
