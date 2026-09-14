# -*- coding: utf-8 -*-
"""Chinese labels for SoilGrids / USDA soil jargon in assessment PDFs."""

from __future__ import annotations

import re
from typing import Any

# USDA texture class → 中文（农户可读）
TEXTURE_ZH: dict[str, str] = {
    "clay": "黏土",
    "heavy clay": "重黏土",
    "clay loam": "黏壤土",
    "silty clay": "粉砂黏土",
    "silty clay loam": "粉砂黏壤土",
    "silt": "粉土",
    "silt loam": "粉壤土",
    "loam": "壤土",
    "sandy loam": "砂壤土",
    "sandy clay loam": "砂黏壤土",
    "sandy clay": "砂黏土",
    "loamy sand": "壤砂土",
    "sand": "砂土",
    "organic": "有机土",
    "peat": "泥炭土",
}

# Drainage class → 中文
DRAINAGE_ZH: dict[str, str] = {
    "excessively drained": "排水过快",
    "somewhat excessively drained": "排水偏快",
    "well drained": "排水良好",
    "moderately well drained": "排水中等偏良",
    "somewhat poorly drained": "排水略差",
    "poorly drained": "排水不良",
    "very poorly drained": "排水极差",
    "imperfectly drained": "排水不完善",
}

AWC_LABEL_ZH = "根系层有效持水量(mm)"
AWC_SHORT_ZH = "根系层有效持水量"

# Longer phrases first so "silty clay loam" wins over "clay"
_TEXTURE_SORTED = sorted(TEXTURE_ZH.keys(), key=len, reverse=True)
_DRAINAGE_SORTED = sorted(DRAINAGE_ZH.keys(), key=len, reverse=True)


def _norm_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def soil_texture_zh(value: Any, *, fallback: str | None = None) -> str:
    """Translate soil texture to Chinese; leave unknown Chinese text as-is."""
    raw = str(value or "").strip()
    if not raw:
        return fallback if fallback is not None else ""
    key = _norm_key(raw)
    if key in TEXTURE_ZH:
        return TEXTURE_ZH[key]
    # Already Chinese or custom label
    if re.search(r"[\u4e00-\u9fff]", raw):
        return raw
    return TEXTURE_ZH.get(key, raw if fallback is None else fallback)


def soil_drainage_zh(value: Any, *, fallback: str | None = None) -> str:
    """Translate drainage class to Chinese."""
    raw = str(value or "").strip()
    if not raw:
        return fallback if fallback is not None else ""
    key = _norm_key(raw)
    if key in DRAINAGE_ZH:
        return DRAINAGE_ZH[key]
    if re.search(r"[\u4e00-\u9fff]", raw):
        return raw
    return DRAINAGE_ZH.get(key, raw if fallback is None else fallback)


def soil_awc_indicator(awc_mm: Any) -> str:
    """e.g. 根系层有效持水量(mm) 164"""
    if awc_mm is None or awc_mm == "":
        return AWC_LABEL_ZH
    try:
        return f"{AWC_LABEL_ZH} {float(awc_mm):.0f}"
    except (TypeError, ValueError):
        return f"{AWC_LABEL_ZH} {awc_mm}"


def translate_soil_jargon(text: Any) -> str:
    """Replace English soil texture/drainage/AWC phrases inside free text."""
    s = str(text or "")
    if not s:
        return s
    # Rootzone AWC variants
    s = re.sub(
        r"(?i)\broot[\s\-]?zone\s*awc\b(?:\s*\(mm\))?",
        AWC_LABEL_ZH,
        s,
    )
    s = re.sub(r"(?i)\bawc\b(?=\s*\d|\s*mm|\s*（|\s*\()", AWC_SHORT_ZH, s)
    for eng in _DRAINAGE_SORTED:
        s = re.sub(re.escape(eng), DRAINAGE_ZH[eng], s, flags=re.IGNORECASE)
    for eng in _TEXTURE_SORTED:
        s = re.sub(re.escape(eng), TEXTURE_ZH[eng], s, flags=re.IGNORECASE)
    return s


def soil_display_fields(soil: dict[str, Any] | None) -> dict[str, Any]:
    """Copy soil dict with Chinese texture/drainage for PDF / LLM facts."""
    soil = dict(soil or {})
    if "dominant_texture" in soil:
        soil["dominant_texture"] = soil_texture_zh(
            soil.get("dominant_texture")
        ) or soil.get("dominant_texture")
    if "drainage_class" in soil:
        soil["drainage_class"] = soil_drainage_zh(
            soil.get("drainage_class")
        ) or soil.get("drainage_class")
    return soil
