"""下载机侧总览预聚合。

API 只负责把一小批原始观测整理成 OSS JSON；下载机从 OSS 拉取后在本地完成
分类和分区汇总，最后把紧凑的结果包交回 API。这样像元 JSON 不会经过 API
响应，也不会让 API 请求一直占用数据库连接。
"""

from __future__ import annotations

from typing import Any

from app.core.agri_classify import (
    NDMI_DRY_ABS,
    PHENOLOGY_MONTHS,
    WEAK_NDVI_LT,
    classify_drought,
    classify_drought_from_pixels,
    classify_flood_series,
    is_drought_season,
    overview_flood_bucket,
)
from app.core.crops import get_crop_season, normalize_crop_key

DROUGHT_KEYS = ("severe", "moderate", "mild", "normal", "unknown")
FLOOD_KEYS = ("flood_severe", "flood_moderate", "flood_mild", "dry", "unknown")
FLOOD_RANK = {None: -1, "dry": 0, "watch": 1, "flood_moderate": 2, "flood_severe": 3}


def _phenology_months(crop: str | None) -> list[int]:
    if crop:
        months = sorted(get_crop_season(crop).season_months)
        if months:
            return months
    return list(PHENOLOGY_MONTHS)


def _pad_adcode(level: str, code: str | None) -> str | None:
    if not code:
        return None
    value = str(code).strip()
    if not value.isdigit():
        return value
    if level == "province" and len(value) <= 2:
        return value.zfill(2) + "0000"
    if level == "city" and len(value) <= 4:
        return value.zfill(4) + "00"
    if level == "county":
        return value.zfill(6)
    return value.zfill(6) if len(value) < 6 else value


def _region_node(level: str, code: str | None, name: str) -> dict[str, Any]:
    return {
        "level": level,
        "code": code,
        "name": name,
    }


def _new_group(
    level: str,
    code: str | None,
    name: str,
    path: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "level": level,
        "code": code,
        "name": name,
        "path": path,
        "children": set(),
        "parcel_count": 0,
        "area_mu": 0.0,
        "drought": {key: 0 for key in DROUGHT_KEYS},
        "drought_area": {key: 0.0 for key in DROUGHT_KEYS},
        "flood": {key: 0 for key in FLOOD_KEYS},
        "flood_area": {key: 0.0 for key in FLOOD_KEYS},
        "weak_count": 0,
        "weak_area": 0.0,
        "pixels_parcels": 0,
        "pixels_classified": 0,
    }


class OverviewAccumulator:
    """Incremental聚合器，只保留各区域计数，不缓存原始像元。"""

    def __init__(self, *, window_from: str, window_to: str, crop: str | None):
        self.window_from = window_from
        self.window_to = window_to
        self.crop = normalize_crop_key(crop) if crop else None
        self.phenology_months = _phenology_months(crop)
        country = _region_node("country", None, "全国")
        self.groups: dict[tuple[str, str], dict[str, Any]] = {
            ("country", ""): _new_group("country", None, "全国", [country])
        }
        self.land_count = 0

    def _ensure_group(
        self,
        *,
        level: str,
        code: str | None,
        name: str,
        path: list[dict[str, Any]],
        parent_key: tuple[str, str] | None,
    ) -> tuple[str, str]:
        key = (level, (code or name).strip())
        if key not in self.groups:
            self.groups[key] = _new_group(level, code, name, path)
        if parent_key is not None:
            self.groups[parent_key]["children"].add(key)
        return key

    @staticmethod
    def _safe_area(value: Any) -> float:
        try:
            return max(0.0, float(value or 0))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _adjust_drought_class(cls: str | None, ndmi: Any) -> str | None:
        if cls in ("mild", "moderate", "severe"):
            try:
                if ndmi is None or float(ndmi) >= NDMI_DRY_ABS:
                    return "normal"
            except (TypeError, ValueError):
                return "normal"
        return cls if cls in DROUGHT_KEYS[:-1] else None

    def _classify_land(self, land: dict[str, Any]) -> dict[str, Any]:
        s2 = land.get("s2") or {}
        scene_cls: str | None = None
        pixel_cls: str | None = None
        pixels_classified = 0
        date_value = str(s2.get("date") or "")[:10]
        if date_value and is_drought_season(date_value):
            scene_cls = self._adjust_drought_class(
                classify_drought(s2.get("ndvi_avg"), s2.get("ndmi_avg")),
                s2.get("ndmi_avg"),
            )
            pixel_data = s2.get("pixel_data")
            if pixel_data is not None and not isinstance(pixel_data, str):
                try:
                    majority, _severe_share, pixel_count = classify_drought_from_pixels(
                        pixel_data
                    )
                except (TypeError, ValueError, KeyError):
                    majority, pixel_count = None, 0
                if majority is not None and pixel_count > 0:
                    pixel_cls = self._adjust_drought_class(
                        majority, s2.get("ndmi_avg")
                    )
                    pixels_classified = pixel_count if pixel_cls else 0

        flood_cls: str | None = None
        observations = land.get("s1") or []
        classified = [
            (day, cls)
            for day, cls in classify_flood_series(observations)
            if cls is not None
        ]
        if classified:
            scene_date, cls = max(
                classified,
                key=lambda item: (item[0], FLOOD_RANK.get(item[1], -1)),
            )
            del scene_date
            flood_cls = overview_flood_bucket(cls)

        return {
            "scene_drought": scene_cls,
            "pixel_drought": pixel_cls,
            "pixels_classified": pixels_classified,
            "flood": flood_cls,
        }

    @staticmethod
    def _add_drought(group: dict[str, Any], cls: str | None, area: float) -> None:
        key = cls or "unknown"
        group["drought"][key] += 1
        group["drought_area"][key] += area

    @staticmethod
    def _add_flood(group: dict[str, Any], cls: str | None, area: float) -> None:
        key = cls or "unknown"
        group["flood"][key] += 1
        group["flood_area"][key] += area

    def add_batch(self, lands: list[dict[str, Any]]) -> None:
        """处理一个 OSS 批次后立即释放原始像元，避免下载机内存持续增长。"""
        country_key = ("country", "")
        for land in lands:
            classification = self._classify_land(land)
            area = self._safe_area(land.get("area_mu"))
            country_group = self.groups[country_key]
            path = [country_group["path"][0]]
            group_keys = [(country_key, "country")]
            parent_key = country_key

            for level, code_field, name_field in (
                ("province", "province_code", "province_name"),
                ("city", "city_code", "city_name"),
                ("county", "county_code", "county_name"),
            ):
                name = land.get(name_field)
                if not name:
                    continue
                code = str(land[code_field]) if land.get(code_field) else None
                node = _region_node(level, code, str(name))
                path = [*path, node]
                key = self._ensure_group(
                    level=level,
                    code=code,
                    name=str(name),
                    path=path,
                    parent_key=parent_key,
                )
                group_keys.append((key, level))
                parent_key = key

            for key, level in group_keys:
                group = self.groups[key]
                group["parcel_count"] += 1
                group["area_mu"] += area
                drought_cls = (
                    classification["scene_drought"]
                    if level == "country"
                    else classification["pixel_drought"]
                    or classification["scene_drought"]
                )
                self._add_drought(group, drought_cls, area)
                self._add_flood(group, classification["flood"], area)
                if land.get("weak"):
                    group["weak_count"] += 1
                    group["weak_area"] += area
                if level != "country" and classification["pixel_drought"]:
                    group["pixels_parcels"] += 1
                    group["pixels_classified"] += classification["pixels_classified"]
            self.land_count += 1

    def merge(self, other: "OverviewAccumulator") -> None:
        """合并独立批次的聚合结果，供并发 OSS 处理后汇总。"""
        if (
            self.window_from != other.window_from
            or self.window_to != other.window_to
            or self.crop != other.crop
        ):
            raise ValueError("总览聚合批次的时间窗口或作物不一致")

        # 每个线程使用独立的聚合器，主线程只在这里合并计数，避免共享聚合器的竞态。
        for key, source in other.groups.items():
            target = self.groups.get(key)
            if target is None:
                self.groups[key] = {
                    **source,
                    "path": [dict(node) for node in source["path"]],
                    "children": set(source["children"]),
                    "drought": dict(source["drought"]),
                    "drought_area": dict(source["drought_area"]),
                    "flood": dict(source["flood"]),
                    "flood_area": dict(source["flood_area"]),
                }
                continue

            target["children"].update(source["children"])
            target["parcel_count"] += source["parcel_count"]
            target["area_mu"] += source["area_mu"]
            for category in DROUGHT_KEYS:
                target["drought"][category] += source["drought"][category]
                target["drought_area"][category] += source["drought_area"][category]
            for category in FLOOD_KEYS:
                target["flood"][category] += source["flood"][category]
                target["flood_area"][category] += source["flood_area"][category]
            target["weak_count"] += source["weak_count"]
            target["weak_area"] += source["weak_area"]
            target["pixels_parcels"] += source["pixels_parcels"]
            target["pixels_classified"] += source["pixels_classified"]

        self.land_count += other.land_count

    @staticmethod
    def _child_summary(group: dict[str, Any]) -> dict[str, Any]:
        drought = group["drought"]
        flood = group["flood"]
        return {
            "level": group["level"],
            "code": group["code"],
            "name": group["name"],
            "parcel_count": group["parcel_count"],
            "area_mu": round(group["area_mu"], 2),
            "drought_severe": drought["severe"],
            "drought_alert": drought["severe"]
            + drought["moderate"]
            + drought["mild"],
            "flood": flood["flood_severe"] + flood["flood_moderate"],
            "flood_alert": flood["flood_severe"] + flood["flood_moderate"],
            "weak_growth": group["weak_count"],
        }

    def results(self) -> list[dict[str, Any]]:
        """生成 API 可直接校验和写入 overview_stats_daily 的紧凑结果。"""
        output: dict[tuple[str, str], dict[str, Any]] = {}
        for key, group in self.groups.items():
            drought = group["drought"]
            drought_area = group["drought_area"]
            flood = group["flood"]
            flood_area = group["flood_area"]
            children = [
                self._child_summary(self.groups[child])
                for child in sorted(
                    group["children"],
                    key=lambda child: (
                        -self.groups[child]["parcel_count"],
                        self.groups[child]["name"],
                    ),
                )
            ]
            output[key] = {
                "region": {
                    "level": group["level"],
                    "code": group["code"],
                    "name": group["name"],
                    "path": group["path"],
                    "adcode": "100000"
                    if group["level"] == "country"
                    else _pad_adcode(group["level"], group["code"]),
                },
                "filters": {
                    "from": self.window_from,
                    "to": self.window_to,
                    "crop": self.crop,
                    "cloud_max_pct": 30.0,
                    "phenology_months": self.phenology_months,
                    "weak_ndvi_lt": WEAK_NDVI_LT,
                    "drought_source": "pixels"
                    if group["pixels_parcels"]
                    else "scene_avg",
                    "cache_hit": False,
                    "pixels_parcels": group["pixels_parcels"],
                    "pixels_classified": group["pixels_classified"],
                },
                "totals": {
                    "parcel_count": group["parcel_count"],
                    "area_mu": round(group["area_mu"], 2),
                },
                "drought": {
                    **drought,
                    "area_mu": {
                        name: round(drought_area[name], 2) for name in DROUGHT_KEYS
                    },
                },
                "flood": {
                    "flood_severe": flood["flood_severe"],
                    "flood_moderate": flood["flood_moderate"],
                    "flood_mild": flood["flood_mild"],
                    "flood": flood["flood_severe"] + flood["flood_moderate"],
                    "wet": flood["flood_mild"],
                    "dry": flood["dry"],
                    "unknown": flood["unknown"],
                    "area_mu": {
                        "flood_severe": round(flood_area["flood_severe"], 2),
                        "flood_moderate": round(flood_area["flood_moderate"], 2),
                        "flood_mild": round(flood_area["flood_mild"], 2),
                        "flood": round(
                            flood_area["flood_severe"] + flood_area["flood_moderate"],
                            2,
                        ),
                        "wet": round(flood_area["flood_mild"], 2),
                        "dry": round(flood_area["dry"], 2),
                        "unknown": round(flood_area["unknown"], 2),
                    },
                },
                "weak_growth": {
                    "parcel_count": group["weak_count"],
                    "area_mu": round(group["weak_area"], 2),
                },
                "children": children,
            }

        return [output[key] for key in sorted(output, key=lambda item: (item[0], item[1]))]


__all__ = ["OverviewAccumulator"]
