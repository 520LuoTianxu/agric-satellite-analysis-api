"""共享遥感质量分算法标识，避免跨服务混用不同统计口径。"""

from __future__ import annotations

import json
from typing import Any

# 地块掩膜内有限像元数 / 地块掩膜内目标格点数。
PARCEL_VALID_FRACTION_V1 = "parcel_mask_valid_fraction_v1"

# 1 - 云量（优先地块云量、否则整景云量），仅是云量启发分，不是像元覆盖率。
CLOUD_COMPLEMENT_HEURISTIC_V1 = "cloud_complement_heuristic_v1"


def extract_quality_score_method(provenance: Any) -> str | None:
    """从RasterLayer来源元数据读取质量分口径；历史缺失值保持未知。"""
    if isinstance(provenance, str):
        try:
            provenance = json.loads(provenance)
        except json.JSONDecodeError:
            return None
    if not isinstance(provenance, dict):
        return None
    method = provenance.get("quality_score_method")
    return method if isinstance(method, str) and method else None
