"""Crop catalog for field binding and land-assessment seasons.

Keys align with ``CROP_PROFILES`` in soil_intelligence. ``fields.crop_type``
stores the catalog key (e.g. ``corn``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.soil_intelligence import CROP_PROFILES

# Chinese display names (fallback to English profile name).
CROP_LABELS_ZH: dict[str, str] = {
    "wheat": "小麦",
    "corn": "玉米",
    "rice": "水稻",
    "barley": "大麦",
    "sorghum": "高粱",
    "millet": "谷子/小米",
    "oats": "燕麦",
    "rye": "黑麦",
    "quinoa": "藜麦",
    "soybean": "大豆",
    "groundnut": "花生",
    "chickpea": "鹰嘴豆",
    "lentil": "扁豆",
    "sunflower": "向日葵",
    "rapeseed": "油菜",
    "sesame": "芝麻",
    "cotton": "棉花",
    "sugarcane": "甘蔗",
    "sugar_beet": "甜菜",
    "tobacco": "烟草",
    "jute": "黄麻",
    "alfalfa": "苜蓿",
    "clover": "三叶草",
    "potato": "马铃薯",
    "cassava": "木薯",
    "sweet_potato": "甘薯",
    "yam": "山药",
    "carrot": "胡萝卜",
    "onion": "洋葱",
    "garlic": "大蒜",
    "tomato": "番茄",
    "pepper": "辣椒",
    "eggplant": "茄子",
    "cucumber": "黄瓜",
    "okra": "秋葵",
    "cabbage": "卷心菜",
    "lettuce": "生菜",
    "spinach": "菠菜",
    "pea": "豌豆",
    "bean": "菜豆",
    "watermelon": "西瓜",
    "pumpkin": "南瓜",
    "banana": "香蕉",
    "mango": "芒果",
    "citrus": "柑橘",
    "grape": "葡萄",
    "apple": "苹果",
    "avocado": "牛油果",
    "papaya": "木瓜",
    "pineapple": "菠萝",
    "strawberry": "草莓",
    "olive": "橄榄",
    "date_palm": "椰枣",
    "coconut": "椰子",
    "coffee": "咖啡",
    "cocoa": "可可",
    "tea": "茶",
    "oil_palm": "油棕",
    "rubber": "橡胶",
    "ginger": "生姜",
    "turmeric": "姜黄",
    "black_pepper": "胡椒",
    "cardamom": "豆蔻",
    "vanilla": "香草",
    "cinnamon": "肉桂",
    "clove": "丁香",
    "nutmeg": "肉豆蔻",
    "saffron": "藏红花",
}

# Aliases → catalog key
CROP_ALIASES: dict[str, str] = {
    "maize": "corn",
    "玉米": "corn",
    "夏玉米": "corn",
    "春玉米": "corn",
    "wheat": "wheat",
    "小麦": "wheat",
    "冬小麦": "wheat",
    "rice": "rice",
    "水稻": "rice",
    "paddy": "rice",
    "soy": "soybean",
    "soybean": "soybean",
    "大豆": "soybean",
    "黄豆": "soybean",
    "cotton": "cotton",
    "棉花": "cotton",
    "peanut": "groundnut",
    "groundnut": "groundnut",
    "花生": "groundnut",
    "rapeseed": "rapeseed",
    "canola": "rapeseed",
    "油菜": "rapeseed",
    "potato": "potato",
    "马铃薯": "potato",
    "土豆": "potato",
    "sorghum": "sorghum",
    "高粱": "sorghum",
    "millet": "millet",
    "谷子": "millet",
    "小米": "millet",
    "tomato": "tomato",
    "番茄": "tomato",
    "西红柿": "tomato",
    "pepper": "pepper",
    "辣椒": "pepper",
    "apple": "apple",
    "苹果": "apple",
    "grape": "grape",
    "葡萄": "grape",
    "citrus": "citrus",
    "柑橘": "citrus",
    "橙子": "citrus",
    "sugarcane": "sugarcane",
    "甘蔗": "sugarcane",
    "tobacco": "tobacco",
    "烟草": "tobacco",
    "sunflower": "sunflower",
    "向日葵": "sunflower",
    "barley": "barley",
    "大麦": "barley",
}


@dataclass(frozen=True)
class CropSeason:
    season_months: frozenset[int]
    peak_months: frozenset[int]
    label_zh: str
    vigor_note: str


_DEFAULT_SEASON = CropSeason(
    season_months=frozenset({6, 7, 8, 9}),
    peak_months=frozenset({7, 8}),
    label_zh="通用生长季（6–9月）",
    vigor_note="按北半球常见夏作季评估绿度；若本地物候不同请后续细化。",
)

# Crop-specific vigor windows (North China / Huang-Huai defaults where noted).
CROP_SEASONS: dict[str, CropSeason] = {
    "corn": CropSeason(
        frozenset({6, 7, 8, 9}),
        frozenset({7, 8}),
        "玉米季（默认6–9月，峰值7–8月；可用自定义生育窗覆盖）",
        "活力按玉米默认生长季，不用全年 NDVI 平均；任务 growing_seasons 优先。",
    ),
    "rice": CropSeason(
        frozenset({6, 7, 8, 9}),
        frozenset({7, 8}),
        "水稻主季（6–9月）",
        "活力按水稻主生育季评估。",
    ),
    "soybean": CropSeason(
        frozenset({6, 7, 8, 9}),
        frozenset({7, 8}),
        "大豆季（6–9月）",
        "活力按大豆生长季评估。",
    ),
    "cotton": CropSeason(
        frozenset({6, 7, 8, 9, 10}),
        frozenset({7, 8, 9}),
        "棉花季（6–10月）",
        "活力按棉花生长季评估。",
    ),
    "wheat": CropSeason(
        frozenset({3, 4, 5, 6}),
        frozenset({4, 5}),
        "小麦季（3–6月，灌浆峰值4–5月）",
        "活力按冬/春小麦返青至成熟季评估，不用夏作 NDVI。",
    ),
    "rapeseed": CropSeason(
        frozenset({3, 4, 5}),
        frozenset({4}),
        "油菜季（3–5月）",
        "活力按油菜花荚期评估。",
    ),
    "barley": CropSeason(
        frozenset({3, 4, 5, 6}),
        frozenset({4, 5}),
        "大麦季（3–6月）",
        "活力按大麦生长季评估。",
    ),
    "potato": CropSeason(
        frozenset({5, 6, 7, 8}),
        frozenset({6, 7}),
        "马铃薯季（5–8月）",
        "活力按马铃薯生长季评估。",
    ),
    "groundnut": CropSeason(
        frozenset({6, 7, 8, 9}),
        frozenset({7, 8}),
        "花生季（6–9月）",
        "活力按花生生长季评估。",
    ),
    "sorghum": CropSeason(
        frozenset({6, 7, 8, 9}),
        frozenset({7, 8}),
        "高粱季（6–9月）",
        "活力按高粱生长季评估。",
    ),
    "millet": CropSeason(
        frozenset({6, 7, 8, 9}),
        frozenset({7, 8}),
        "谷子季（6–9月）",
        "活力按谷子生长季评估。",
    ),
    "sunflower": CropSeason(
        frozenset({6, 7, 8, 9}),
        frozenset({7, 8}),
        "向日葵季（6–9月）",
        "活力按向日葵生长季评估。",
    ),
    "tomato": CropSeason(
        frozenset({5, 6, 7, 8, 9}),
        frozenset({6, 7, 8}),
        "番茄季（5–9月）",
        "活力按番茄生长季评估。",
    ),
    "pepper": CropSeason(
        frozenset({5, 6, 7, 8, 9}),
        frozenset({6, 7, 8}),
        "辣椒季（5–9月）",
        "活力按辣椒生长季评估。",
    ),
    "apple": CropSeason(
        frozenset({5, 6, 7, 8, 9}),
        frozenset({6, 7, 8}),
        "苹果生长季（5–9月）",
        "果树按叶片生长季评估绿度。",
    ),
    "grape": CropSeason(
        frozenset({5, 6, 7, 8, 9}),
        frozenset({6, 7, 8}),
        "葡萄生长季（5–9月）",
        "果树按叶片生长季评估绿度。",
    ),
    "citrus": CropSeason(
        frozenset({4, 5, 6, 7, 8, 9, 10}),
        frozenset({6, 7, 8}),
        "柑橘生长季（4–10月）",
        "果树按叶片生长季评估绿度。",
    ),
    "sugarcane": CropSeason(
        frozenset({5, 6, 7, 8, 9, 10, 11}),
        frozenset({7, 8, 9}),
        "甘蔗季（5–11月）",
        "活力按甘蔗主生长季评估。",
    ),
    "tobacco": CropSeason(
        frozenset({5, 6, 7, 8}),
        frozenset({6, 7}),
        "烟草季（5–8月）",
        "活力按烟草生长季评估。",
    ),
}


def normalize_crop_key(raw: str | None) -> str | None:
    """Map free text / alias / key → catalog key, or None if unknown."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    low = s.lower().replace(" ", "_").replace("/", "_").replace("-", "_")
    if low in CROP_PROFILES:
        return low
    if s in CROP_ALIASES:
        return CROP_ALIASES[s]
    if low in CROP_ALIASES:
        return CROP_ALIASES[low]
    # Chinese labels reverse lookup
    for key, zh in CROP_LABELS_ZH.items():
        if s == zh or zh in s:
            return key
    return None


def require_crop_key(raw: str | None) -> str:
    key = normalize_crop_key(raw)
    if not key:
        raise ValueError(f"Unknown crop: {raw!r}. Use GET /v1/crops for valid keys.")
    return key


def get_crop_season(key: str | None) -> CropSeason:
    k = normalize_crop_key(key) or ""
    return CROP_SEASONS.get(k, _DEFAULT_SEASON)


def crop_name_zh(key: str | None) -> str:
    k = normalize_crop_key(key)
    if not k:
        return "未知作物"
    if k in CROP_LABELS_ZH:
        return CROP_LABELS_ZH[k]
    prof = CROP_PROFILES.get(k)
    return prof.name if prof else k


def crop_name_en(key: str | None) -> str:
    k = normalize_crop_key(key)
    if not k:
        return "Unknown"
    prof = CROP_PROFILES.get(k)
    return prof.name if prof else k


def list_crops() -> list[dict[str, Any]]:
    """Sorted catalog for UI select."""
    out: list[dict[str, Any]] = []
    for key in sorted(CROP_PROFILES.keys()):
        season = get_crop_season(key)
        out.append(
            {
                "key": key,
                "name": crop_name_en(key),
                "name_zh": crop_name_zh(key),
                "season_months": sorted(season.season_months),
                "peak_months": sorted(season.peak_months),
                "season_label_zh": season.label_zh,
            }
        )
    return out
