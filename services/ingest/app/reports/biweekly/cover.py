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
import re

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
# Footer / disclaimer ink: dark enough to read on the pale mint band, quieter than the title.
FOOTER = (45, 72, 58)
FOOTER_RULE = (31, 77, 56)
FOOTER_HEADING_FILL = PRIMARY  # same dark green as the cover title
FOOTER_CONTACT_FILL = (45, 62, 52)  # dark green-gray; never blue
FOOTER_DISCLAIMER_FILL = (102, 102, 102)  # medium gray, readable on mint


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


# Cover-footer wording is fixed by the issuing unit after user review.
# Contacts sit above a short disclaimer in the lower information band.
# Do not paraphrase, reorder, or change punctuation, names, or email addresses.
COVER_FOOTER_ORG = "数字农业中心｜报告咨询"
COVER_FOOTER_CONTACT_LIU = "刘伯恩：liuboen@chongho.net"
COVER_FOOTER_CONTACT_LUO = "罗添煦：luotianxu@chongho.net"
COVER_FOOTER_DISCLAIMER = "部分内容由 AI 辅助生成，仅供参考，地块实际情况以实地核查为准。"
COVER_FOOTER_CONTACTS: tuple[str, ...] = (
    COVER_FOOTER_CONTACT_LIU,
    COVER_FOOTER_CONTACT_LUO,
)
# Display order: organisation heading, two contacts, then the short disclaimer.
COVER_FOOTER_PARAGRAPHS: tuple[str, ...] = (
    COVER_FOOTER_ORG,
    COVER_FOOTER_CONTACT_LIU,
    COVER_FOOTER_CONTACT_LUO,
    COVER_FOOTER_DISCLAIMER,
)

# Layout at 200 dpi, compact but readable, pinned to the pale mint band.
FOOTER_HEADING_SIZE = 30
FOOTER_CONTACT_SIZE = 28
FOOTER_DISCLAIMER_SIZE = 28
FOOTER_FONT_SIZE = FOOTER_CONTACT_SIZE  # alias for tests / single-font fallback
FOOTER_HEADING_GAP = 10
FOOTER_CONTACT_GAP = 8
FOOTER_GAP_BEFORE_DISCLAIMER = 18
FOOTER_RULE_HEIGHT = 2
FOOTER_RULE_GAP = 16
FOOTER_RULE_WIDTH = 320
FOOTER_BOTTOM_MARGIN = 72
FOOTER_SIDE_MARGIN = 110
FOOTER_PREFERRED_MIN_TOP = 2100

_FOOTER_WRAP_UNIT = re.compile(r"[\x00-\x7F]+|.")
# Prefer ending a line after these marks so CJK wording is not split mid-phrase.
_FOOTER_BREAK_AFTER = set("，。；：、！？）》")


def measure_text_width(font, text: str) -> float:
    """Advance width of ``text`` in the given PIL font (no drawing)."""
    left, _top, right, _bottom = font.getbbox(text or "")
    return float(right - left)


def centered_text_x(font, text: str, canvas_width: int) -> int:
    """Left coordinate that centres ``text`` on the cover."""
    width = measure_text_width(font, text)
    return int(round((canvas_width - width) / 2))


def wrap_footer_text(text: str, font, max_width: float) -> list[str]:
    """Wrap without altering characters. ASCII runs (e.g. emails) stay together."""
    if not text:
        return [""]
    units = _FOOTER_WRAP_UNIT.findall(text)
    lines: list[str] = []
    current = ""
    for unit in units:
        trial = current + unit
        if current and measure_text_width(font, trial) > max_width:
            break_at = max(
                (i for i, ch in enumerate(current) if ch in _FOOTER_BREAK_AFTER),
                default=-1,
            )
            if break_at >= 0 and break_at + 1 >= max(8, len(current) // 4):
                lines.append(current[: break_at + 1])
                current = current[break_at + 1 :] + unit
            else:
                lines.append(current)
                current = unit
        else:
            current = trial
    if current:
        lines.append(current)
    fitted: list[str] = []
    for line in lines:
        if measure_text_width(font, line) <= max_width or len(line) == 1:
            fitted.append(line)
            continue
        buf = ""
        for ch in line:
            trial = buf + ch
            if buf and measure_text_width(font, trial) > max_width:
                fitted.append(buf)
                buf = ch
            else:
                buf = trial
        if buf:
            fitted.append(buf)
    return fitted or [""]


def cover_footer_rows(contact_font, disclaimer_font, max_width: float) -> list[tuple[str, str]]:
    """Return (kind, text) rows: heading, contacts, a blank, then the disclaimer.

    Kind is one of heading / contact / blank / disclaimer. Disclaimer wording is
    preserved; only line breaks may be inserted if the line is too long.
    """
    rows: list[tuple[str, str]] = [
        ("heading", COVER_FOOTER_ORG),
        ("contact", COVER_FOOTER_CONTACT_LIU),
        ("contact", COVER_FOOTER_CONTACT_LUO),
        ("blank", ""),
    ]
    for line in wrap_footer_text(COVER_FOOTER_DISCLAIMER, disclaimer_font, max_width):
        rows.append(("disclaimer", line))
    return rows


def _row_advance(kind: str) -> int:
    if kind == "blank":
        return FOOTER_GAP_BEFORE_DISCLAIMER
    if kind == "heading":
        return FOOTER_HEADING_SIZE + FOOTER_HEADING_GAP
    if kind == "contact":
        return FOOTER_CONTACT_SIZE + FOOTER_CONTACT_GAP
    return FOOTER_DISCLAIMER_SIZE + FOOTER_CONTACT_GAP


def cover_footer_geometry(
    font=None,
    *,
    heading_font=None,
    contact_font=None,
    disclaimer_font=None,
    canvas_width: int = COVER_W,
    canvas_height: int = COVER_H,
) -> dict[str, Any]:
    """Compute footer placement pinned to the bottom mint band, within the canvas."""
    contact = contact_font or font
    disclaimer = disclaimer_font or font
    if contact is None or disclaimer is None:
        raise ValueError("cover footer fonts are required")
    x0 = FOOTER_SIDE_MARGIN
    max_width = canvas_width - 2 * FOOTER_SIDE_MARGIN
    rows = cover_footer_rows(contact, disclaimer, max_width)
    content_h = sum(_row_advance(kind) for kind, _text in rows)
    block_h = FOOTER_RULE_HEIGHT + FOOTER_RULE_GAP + content_h
    bottom = FOOTER_BOTTOM_MARGIN
    top = canvas_height - bottom - block_h
    if top < 0:
        top = 0
    return {
        "x": x0,
        "max_width": max_width,
        "top": int(top),
        "bottom": int(top + block_h),
        "rule_y": int(top),
        "rows": rows,
        "block_height": int(block_h),
        "canvas_width": canvas_width,
        "canvas_height": canvas_height,
        "heading_font": heading_font or font,
        "contact_font": contact,
        "disclaimer_font": disclaimer,
    }


def draw_cover_footer(
    draw,
    font=None,
    *,
    heading_font=None,
    contact_font=None,
    disclaimer_font=None,
    canvas_width: int = COVER_W,
    canvas_height: int = COVER_H,
) -> dict[str, Any]:
    """Paint a compact, centred footer: rule, short title, contacts, gray disclaimer."""
    geo = cover_footer_geometry(
        font,
        heading_font=heading_font,
        contact_font=contact_font,
        disclaimer_font=disclaimer_font,
        canvas_width=canvas_width,
        canvas_height=canvas_height,
    )
    rule_y = geo["rule_y"]
    rule_x = int(round((canvas_width - FOOTER_RULE_WIDTH) / 2))
    draw.rectangle(
        (rule_x, rule_y, rule_x + FOOTER_RULE_WIDTH, rule_y + FOOTER_RULE_HEIGHT),
        fill=FOOTER_RULE,
    )
    y = rule_y + FOOTER_RULE_HEIGHT + FOOTER_RULE_GAP
    fonts = {
        "heading": geo["heading_font"] or geo["contact_font"],
        "contact": geo["contact_font"],
        "disclaimer": geo["disclaimer_font"],
    }
    fills = {
        "heading": FOOTER_HEADING_FILL,
        "contact": FOOTER_CONTACT_FILL,
        "disclaimer": FOOTER_DISCLAIMER_FILL,
    }
    bboxes: list[tuple[int, int, int, int]] = []
    centers: list[float] = []
    for kind, text_line in geo["rows"]:
        if kind == "blank":
            y += _row_advance(kind)
            continue
        used = fonts[kind]
        x = centered_text_x(used, text_line, canvas_width)
        draw.text((x, y), text_line, font=used, fill=fills[kind])
        box = draw.textbbox((x, y), text_line, font=used)
        bboxes.append(box)
        centers.append((box[0] + box[2]) / 2)
        y += _row_advance(kind)
    geo["text_bboxes"] = bboxes
    geo["line_centers"] = centers
    geo["rule_x"] = rule_x
    geo["drawn_bottom"] = int(y)
    return geo


def assert_footer_wording_intact(rows: list[tuple[str, str]]) -> None:
    """Raise ValueError if laid-out lines no longer equal the official wording."""
    kinds = [kind for kind, _text in rows]
    if kinds[:3] != ["heading", "contact", "contact"]:
        raise ValueError("cover footer must start with the organisation heading and two contacts")
    if "blank" not in kinds:
        raise ValueError("cover footer is missing the gap before the disclaimer")
    if kinds.index("blank") != 3:
        raise ValueError("disclaimer must come after the contact block")
    got = {kind: "" for kind in ("heading", "contact", "disclaimer")}
    contacts: list[str] = []
    for kind, text_line in rows:
        if kind == "blank":
            continue
        if kind == "contact":
            contacts.append(text_line)
        else:
            got[kind] += text_line
    if got["heading"] != COVER_FOOTER_ORG:
        raise ValueError("cover footer organisation heading diverged")
    if contacts != list(COVER_FOOTER_CONTACTS):
        raise ValueError("cover footer contacts diverged from the official names or emails")
    if got["disclaimer"] != COVER_FOOTER_DISCLAIMER:
        raise ValueError("cover footer disclaimer diverged from the official wording")


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

    heading_font = ImageFont.truetype(str(bold), size=FOOTER_HEADING_SIZE)
    contact_font = ImageFont.truetype(str(regular), size=FOOTER_CONTACT_SIZE)
    disclaimer_font = ImageFont.truetype(str(regular), size=FOOTER_DISCLAIMER_SIZE)
    footer_geo = draw_cover_footer(
        draw,
        heading_font=heading_font,
        contact_font=contact_font,
        disclaimer_font=disclaimer_font,
        canvas_width=COVER_W,
        canvas_height=COVER_H,
    )
    assert_footer_wording_intact(footer_geo["rows"])
    # Keep the footer in the lower band and clear of the meta block.
    if footer_geo["top"] < y + 36:
        raise RuntimeError("cover footer would overlap cover meta rows")
    for box in footer_geo["text_bboxes"]:
        left, top, right, bottom = box
        if left < 0 or top < 0 or right > COVER_W or bottom > COVER_H:
            raise RuntimeError(f"cover footer overflows the canvas: {box}")

    canvas.save(dest_path, "PNG")
    return dest_path
