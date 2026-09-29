"""跨 API、ingest 与 Web 仓库静态核对遥感分类共享阈值。"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any


API_ROOT = Path(__file__).resolve().parents[1]
API_CLASSIFIER = API_ROOT / "services/api/app/core/agri_classify.py"
INGEST_CLASSIFIER = API_ROOT / "services/ingest/app/core/agri_classify.py"
WEB_CLASSIFIER_DEFAULT = (
    API_ROOT.parent / "agric-satellite-analysis-web" / "src/lib/agri-classify.ts"
)

# 这些值同时控制后端产品择景、风险分级与前端风险提示，必须保持一致。
SHARED_RULE_NAMES = (
    "CLOUD_MAX_PCT",
    "PHENOLOGY_MONTHS",
    "PARCEL_CLOUD_SOURCE_SCL",
    "PARCEL_CLOUD_SOURCE_LONLAT",
    "LEGACY_PARCEL_CLOUD_MIN",
    "LEGACY_STAC_CLOUD_MAX",
    "LEGACY_PARCEL_STAC_GAP",
    "CLEAR_PIXEL_FRACTION_TRUST",
    "SUSPICIOUS_PARCEL_VS_CLEAR",
    "SUSPICIOUS_CLEAR_PARCEL_MAX",
    "SUSPICIOUS_STAC_OVERCAST_MIN",
    "SUSPICIOUS_STAC_OVER_PARCEL_GAP",
    "SUSPICIOUS_STAC_MIN",
    "NEARBY_CLEAR_DAYS",
    "BORDERLINE_PARCEL_MIN",
    "BORDERLINE_PARCEL_MAX",
    "PHYSIOLOGY_NDVI_ABSURD_GAP",
    "PHYSIOLOGY_GROWING_NDVI_FLOOR",
    "PHYSIOLOGY_GREEN_NEIGHBOR_NDVI",
    "NDDI_MILD_MIN",
    "NDDI_MODERATE_MIN",
    "NDDI_SEVERE_MIN",
    "NDMI_FALLBACK_SEVERE",
    "NDDI_PCTL_DRY",
    "MIN_MONTH_SAMPLES",
    "NDMI_DRY_ABS",
    "NDMI_DROP_VS_MEDIAN",
    "NDVI_DROP_VS_MEDIAN",
    "NDVI_DROP_MODERATE",
    "NDVI_DROP_SEVERE",
    "FLOOD_VV_MAX",
    "FLOOD_VV_DROP",
    "FLOOD_VH_MAX",
    "WATCH_VV_MAX",
    "WATCH_VV_DROP",
    "WATCH_VH_MAX",
    "FLOOD_VV_SEVERE",
    "MIN_ORBIT_SAMPLES",
    "VV_VH_DIFF_PCTL",
    "FLOOD_SPRING_MONTHS",
    "DECLOUD_SOURCE",
    "DECLOUD_SCENE_ID_SUFFIX",
)


def _python_value(node: ast.expr, values: dict[str, Any]) -> Any:
    """解析分类常量的安全 AST 子集，不执行被检查文件中的代码。"""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _python_value(node.operand, values)
        if isinstance(value, (int, float)):
            return value if isinstance(node.op, ast.UAdd) else -value
        raise ValueError("unary sign only supports numeric classifier constants")
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.Tuple):
        return tuple(_python_value(item, values) for item in node.elts)
    if isinstance(node, ast.List):
        return [_python_value(item, values) for item in node.elts]
    if isinstance(node, ast.Set):
        return {_python_value(item, values) for item in node.elts}
    if isinstance(node, ast.Dict):
        return {
            _python_value(key, values): _python_value(value, values)
            for key, value in zip(node.keys, node.values, strict=True)
        }
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and len(node.args) == 1
    ):
        return frozenset(_python_value(node.args[0], values))
    raise ValueError(f"unsupported Python constant expression: {ast.dump(node)}")


def _python_assignments(path: Path) -> dict[str, Any]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                try:
                    values[target.id] = _python_value(node.value, values)
                except (KeyError, ValueError):
                    continue
    return values


def _typescript_literals(path: Path) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"^export const (?P<name>[A-Z][A-Z0-9_]*)\s*=\s*(?P<value>[^;\n]+);?$",
        re.MULTILINE,
    )
    values: dict[str, Any] = {}
    for match in pattern.finditer(source):
        raw = re.sub(r"\s+as const\s*$", "", match.group("value")).strip()
        try:
            values[match.group("name")] = json.loads(raw)
        except json.JSONDecodeError:
            continue
    return values


def _normalized(value: Any) -> Any:
    if isinstance(value, (tuple, list)):
        return [_normalized(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_normalized(item) for item in value)
    return value


def _check(web_classifier: Path) -> list[str]:
    errors: list[str] = []
    if API_CLASSIFIER.read_bytes() != INGEST_CLASSIFIER.read_bytes():
        errors.append("API 与 ingest 的 agri_classify.py 内容不一致")

    python_values = _python_assignments(API_CLASSIFIER)
    web_values = _typescript_literals(web_classifier)
    missing_python = [name for name in SHARED_RULE_NAMES if name not in python_values]
    missing_web = [name for name in SHARED_RULE_NAMES if name not in web_values]
    if missing_python:
        errors.append(f"Python 缺少共享规则：{', '.join(missing_python)}")
    if missing_web:
        errors.append(f"Web 缺少共享规则：{', '.join(missing_web)}")

    mismatches = {}
    for name in SHARED_RULE_NAMES:
        if name not in python_values or name not in web_values:
            continue
        python_value = _normalized(python_values[name])
        web_value = _normalized(web_values[name])
        if python_value != web_value:
            mismatches[name] = {"python": python_value, "web": web_value}
    if mismatches:
        errors.append(
            "前后端共享规则不一致："
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True)
        )

    trusted_names = re.search(
        r"export const PARCEL_CLOUD_SOURCES_TRUSTED = new Set\(\[(.*?)\]\);",
        web_classifier.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    if "PARCEL_CLOUD_SOURCES_TRUSTED" not in python_values:
        errors.append("Python 缺少 PARCEL_CLOUD_SOURCES_TRUSTED")
    elif trusted_names is None:
        errors.append("Web 缺少 PARCEL_CLOUD_SOURCES_TRUSTED")
    else:
        names = re.findall(r"PARCEL_CLOUD_SOURCE_[A-Z_]+", trusted_names.group(1))
        missing_values = [name for name in names if name not in web_values]
        if missing_values:
            errors.append(
                f"Web trusted cloud source 未定义：{', '.join(missing_values)}"
            )
        else:
            python_trusted = _normalized(python_values["PARCEL_CLOUD_SOURCES_TRUSTED"])
            web_trusted = sorted(web_values[name] for name in names)
            if python_trusted != web_trusted:
                errors.append(
                    "可信地块云量来源不一致："
                    + json.dumps(
                        {"python": python_trusted, "web": web_trusted},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--web-classifier",
        type=Path,
        default=WEB_CLASSIFIER_DEFAULT,
        help="Web 仓库 agri-classify.ts 路径，默认使用相邻工作区",
    )
    args = parser.parse_args()
    if not args.web_classifier.is_file():
        print(f"Web 分类文件不存在：{args.web_classifier}", file=sys.stderr)
        return 2

    errors = _check(args.web_classifier)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(
        f"分类口径一致：{len(SHARED_RULE_NAMES)} 项阈值、可信云量来源，"
        "API/ingest 文件逐字节一致。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
