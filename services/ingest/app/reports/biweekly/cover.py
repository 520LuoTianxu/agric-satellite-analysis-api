"""Compose the official biweekly cover from bundled artwork.

The background and logo live next to the bundled YaHei fonts under
``reports/biweekly/assets/``. official_pdf reads them from this package path;
there is no environment variable or external path to configure.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
BUNDLED_ASSET_DIR = Path(__file__).resolve().parent / "assets"
BACKGROUND_NAME = "cover_background.png"
LOGO_NAME = "xianghe_logo.png"

# A4 at 200 dpi, same as the finalized v8 cover.
COVER_W, COVER_H = 1654, 2339
PRIMARY = (31, 77, 56)
SUB = (122, 122, 122)
LABEL = (140, 140, 140)
VALUE = (35, 60, 48)


def bundled_asset_dir() -> Path:
    return BUNDLED_ASSET_DIR


def resolve_cover_assets() -> tuple[Path, Path | None]:
    """Return (background, logo) from the packaged assets directory only."""
    directory = bundled_asset_dir()
    background = directory / BACKGROUND_NAME
    logo = directory / LOGO_NAME
    if not background.is_file():
        raise FileNotFoundError(str(background))
    return background, logo if logo.is_file() else None


def _as_day(value: Any) -> str:
    return str(value or "")[:10]


def _period(facts: dict[str, Any]) -> dict[str, Any]:
    return dict(facts.get("period") or {})


def _phase(facts: dict[str, Any]) -> dict[str, Any]:
    return dict(facts.get("phase") or {})


def compact_date_span(period: dict[str, Any]) -> str:
    """v8-style span: 2026年4月4日—17日 when the month is the same."""
    start = _as_day(period.get("start"))
    end = _as_day(period.get("end_inclusive") or period.get("end"))
    if not end:
        exclusive = _as_day(period.get("end_exclusive"))
        if exclusive:
            from datetime import timedelta

            end = (date.fromisoformat(exclusive) - timedelta(days=1)).isoformat()
    if len(start) != 10:
        return start
    sy, sm, sd = int(start[:4]), int(start[5:7]), int(start[8:10])
    if len(end) != 10:
        return f"{sy}年{sm}月{sd}日"
    ey, em, ed = int(end[:4]), int(end[5:7]), int(end[8:10])
    if sy == ey and sm == em:
        return f"{sy}年{sm}月{sd}日—{ed}日"
    if sy == ey:
        return f"{sy}年{sm}月{sd}日—{em}月{ed}日"
    return f"{sy}年{sm}月{sd}日—{ey}年{em}月{ed}日"


def _report_year(period: dict[str, Any]) -> int:
    start = _as_day(period.get("start"))
    if len(start) >= 4 and start[:4].isdigit():
        return int(start[:4])
    return datetime.now(SHANGHAI).year


def _report_date_label(facts: dict[str, Any]) -> str:
    raw = facts.get("report_date") or facts.get("generated_at")
    stamp = _as_day(raw)
    if len(stamp) == 10:
        day = date.fromisoformat(stamp)
    else:
        day = datetime.now(SHANGHAI).date()
    return f"{day.year}年{day.month}月{day.day}日"


def _crop_label(facts: dict[str, Any]) -> str | None:
    phase = _phase(facts)
    raw = (
        facts.get("crop_name")
        or facts.get("crop")
        or (facts.get("land") or {}).get("crop_name")
        or phase.get("crop_name")
    )
    if not raw:
        return None
    name = str(raw).strip()
    if not name:
        return None
    if name in {"小麦", "冬小麦"}:
        name = "冬小麦"
    if phase.get("plan_source") == "reference" or phase.get("phenology_basis") == "PLAN_DERIVED_FROM_BTU_REFERENCE":
        return f"{name}（参照方案）"
    if phase.get("plan_source") == "native" or phase.get("plan_label") == "本田方案":
        return f"{name}（本田方案）"
    return name


def _phase_label(facts: dict[str, Any]) -> str | None:
    """Stage shown on the cover, in formal words: 拔节孕穗期（营养生长与生殖生长并进阶段）."""
    from agric_satellite_analysis_common.biweekly.phase_policy import PHASE_FORMAL_NAMES

    phase = _phase(facts)
    stages: list[str] = []
    labels: list[str] = []
    for row in phase.get("observation_stages") or []:
        code = row.get("stage_code")
        if code and code not in stages:
            stages.append(code)
        label = row.get("phase_label")
        if label and label not in labels:
            labels.append(label)
    # 阶段代号 S1/S2/S3 不上封面，只写中文阶段名；跨阶段时不合并成一个阶段名。
    phase_name = PHASE_FORMAL_NAMES.get(labels[0]) if len(labels) == 1 else None
    if not stages:
        return PHASE_FORMAL_NAMES.get(phase.get("display_phase"))
    text = "、".join(stages)
    return f"{text}（{phase_name}）" if phase_name else text


def _location_label(facts: dict[str, Any]) -> str | None:
    land = facts.get("land") or {}
    for key in ("location", "address", "region", "admin_location"):
        value = facts.get(key) or land.get(key)
        if value:
            return str(value).strip() or None
    parts = [
        facts.get("province") or land.get("province"),
        facts.get("city") or land.get("city"),
        facts.get("county") or land.get("county") or facts.get("district") or land.get("district"),
    ]
    text = "".join(str(part).strip() for part in parts if part)
    return text or None


def _scale_label(facts: dict[str, Any]) -> str | None:
    land = facts.get("land") or {}
    mu = facts.get("area_mu") or facts.get("land_area_mu") or land.get("area_mu") or land.get("mu")
    if mu is None:
        return None
    try:
        value = float(mu)
    except (TypeError, ValueError):
        return str(mu)
    text = f"登记{value:.2f}亩".replace(".00亩", "亩")
    blocks = facts.get("block_count") or land.get("block_count")
    if blocks:
        text += f"  ·  {int(blocks)}块地"
    return text


def cover_meta_rows(facts: dict[str, Any]) -> list[tuple[str, str]]:
    """Only rows with known values; never invent location, area, or a default plan."""
    rows: list[tuple[str, str]] = []
    location = _location_label(facts)
    if location:
        rows.append(("位置", location))
    crop = _crop_label(facts)
    if crop:
        rows.append(("作物", crop))
    phase = _phase_label(facts)
    if phase:
        rows.append(("阶段", phase))
    scale = _scale_label(facts)
    if scale:
        rows.append(("规模", scale))
    rows.append(("报告日期", _report_date_label(facts)))
    if str(facts.get("data_kind") or "").lower() == "demo":
        # 示例数据必须在封面上写明，不能让样稿被当成实测报告。
        rows.append(("数据性质", "示例数据，非实测，不用于农事决策"))
    return rows


def compose_official_cover(facts: dict[str, Any], dest: str | Path, *, land_title: str | None = None) -> Path:
    """Paint land-specific cover text onto the bundled formal background."""
    from PIL import Image, ImageDraw, ImageFont

    from app.reports.biweekly.fonts import resolve_yahei_fonts

    background, logo_path = resolve_cover_assets()
    regular, bold = resolve_yahei_fonts()
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    period = _period(facts)
    title_name = land_title or str(facts.get("land_name") or "").strip() or "地块"
    year = _report_year(period)

    bg = Image.open(background).convert("RGB")
    canvas = bg.resize((COVER_W, COVER_H), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(canvas)
    x0, y0 = 110, 150
    if logo_path is not None:
        logo = Image.open(logo_path).convert("RGBA").resize((92, 92), Image.Resampling.LANCZOS)
        canvas.paste(logo, (x0, y0), logo)
        text_x = x0 + 92 + 18
    else:
        text_x = x0
    fb = ImageFont.truetype(str(bold), size=48)
    fr = ImageFont.truetype(str(regular), size=24)
    draw.text((text_x, y0 + 6), "乡合农服", font=fb, fill=PRIMARY)
    draw.text((text_x, y0 + 62), "种植双周报  ·  地块维度", font=fr, fill=SUB)

    ft = ImageFont.truetype(str(bold), size=86)
    title = f"{year}年种植双周长势报告"
    ty = 520
    draw.text((x0, ty), title, font=ft, fill=PRIMARY)
    draw.rectangle((x0, ty + 130, x0 + 210, ty + 138), fill=PRIMARY)
    fn = ImageFont.truetype(str(bold), size=44)
    draw.text((x0, ty + 170), title_name, font=fn, fill=PRIMARY)
    fp = ImageFont.truetype(str(regular), size=30)
    draw.text((x0, ty + 240), compact_date_span(period), font=fp, fill=PRIMARY)

    fl = ImageFont.truetype(str(regular), size=30)
    fv = ImageFont.truetype(str(bold), size=30)
    y = 960
    for label, value in cover_meta_rows(facts):
        draw.text((x0, y), label, font=fl, fill=LABEL)
        draw.text((x0 + 260, y), value, font=fv, fill=VALUE)
        y += 74

    canvas.save(dest_path, "PNG")
    return dest_path
