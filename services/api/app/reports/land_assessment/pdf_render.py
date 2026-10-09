# -*- coding: utf-8 -*-
"""地块遥感选地评估报告：封面摘要 + 十章正文 + 数据方法说明，按版面自然分页。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    CondPageBreak,
    Flowable,
    HRFlowable,
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

from app.core.crops import crop_name_zh
from app.reports.land_assessment.paths import FONT_PATH
from app.reports.land_assessment.soil_labels import (
    AWC_LABEL_ZH,
    soil_drainage_zh,
    soil_texture_zh,
    translate_soil_jargon,
)

LIGHT_WORD = {"绿": "适宜", "黄": "基本适宜", "红": "需关注"}
LIGHT_COLOR = {"绿": "#1B7A3D", "黄": "#B7791F", "红": "#C0392B"}
LIGHT_BG = {"绿": "#EAF5EE", "黄": "#FDF5E3", "红": "#FCEBEA"}
AI_FAIL = "AI 分析失败"

PRIMARY = "#1F4D38"
INK = "#263238"
MUTED = "#607D8B"
RULE = "#D5DED8"
ZEBRA = "#F5F8F6"

CONTENT_W = 168 * mm

CST = timezone(timedelta(hours=8))

DIM_ORDER = ("crop", "soil", "vigor", "weather", "wet_safety", "drought_safety")
DIM_TITLE = {
    "crop": "作物匹配",
    "soil": "土壤条件",
    "vigor": "遥感长势",
    "weather": "天气适宜",
    "wet_safety": "抗渍/洪涝",
    "drought_safety": "抗旱安全",
}

# Major chapters (cover 摘要即第一章；目录页不编号)
TOC_ENTRIES = [
    ("一、综合评价", "综合评分、评估结论与核心建议"),
    ("二、地块基础画像", "位置、土壤、气候与现场条件"),
    ("三、综合评分解释", "六维评分结构与改进方向"),
    ("四、遥感长势", "植被指数时序、物候阶段与异常事件"),
    ("五、空间异常", "物候阶段空间分布与关注区域"),
    ("六、土壤", "土壤指标与田间影响"),
    ("七、气候风险", "历史气候与渍涝、干旱证据"),
    ("八、种植管理建议", "品种、播种、水肥与巡田"),
    ("九、产量潜力", "相对等级评估"),
    ("十、经营分析", "经营测算与数据方法说明"),
]

SATELLITE_INDICES = (
    ("NDVI", "(NIR - Red) / (NIR + Red)", "冠层绿度与生物量，主要长势指标"),
    ("EVI", "2.5 × (NIR - Red) / (NIR + 6·Red - 7.5·Blue + 1)", "高覆盖条件下的长势，缓解 NDVI 饱和"),
    ("NDWI", "(Green - NIR) / (Green + NIR)", "地表水体与湿润程度，用于渍涝识别"),
)


def _register_fonts() -> None:
    if "CN" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("CN", str(FONT_PATH)))
        pdfmetrics.registerFont(TTFont("CNB", str(FONT_PATH)))


def _styles() -> dict[str, ParagraphStyle]:
    def style(name: str, **kw) -> ParagraphStyle:
        # CJK 断行按字切分；两端对齐会把中英混排处的空格拉宽，故正文统一左对齐。
        base = {
            "fontName": "CN",
            "fontSize": 9.5,
            "leading": 15,
            "textColor": HexColor(INK),
            "wordWrap": "CJK",
        }
        base.update(kw)
        return ParagraphStyle(name, **base)

    return {
        "cover_brand": style(
            "cover_brand", fontName="CNB", fontSize=9, leading=12, textColor=HexColor(PRIMARY)
        ),
        "cover_title": style(
            "cover_title",
            fontName="CNB",
            fontSize=21,
            leading=28,
            alignment=TA_LEFT,
            textColor=HexColor(PRIMARY),
        ),
        "cover_sub": style(
            "cover_sub", fontSize=10.5, leading=15, alignment=TA_LEFT, textColor=HexColor(MUTED)
        ),
        "h1": style(
            "h1",
            fontName="CNB",
            fontSize=14,
            leading=20,
            textColor=HexColor(PRIMARY),
            spaceBefore=6,
            spaceAfter=2,
            keepWithNext=True,
        ),
        "h2": style(
            "h2",
            fontName="CNB",
            fontSize=10.5,
            leading=15,
            textColor=HexColor(PRIMARY),
            spaceBefore=5,
            spaceAfter=3,
            keepWithNext=True,
        ),
        "body": style("body", alignment=TA_LEFT),
        "small": style("small", fontSize=8, leading=12, textColor=HexColor(MUTED)),
        "caption": style(
            "caption",
            fontSize=8,
            leading=11,
            alignment=TA_CENTER,
            textColor=HexColor("#455A64"),
            spaceBefore=1,
            spaceAfter=4,
        ),
        "tcaption": style(
            "tcaption",
            fontName="CNB",
            fontSize=8.5,
            leading=12,
            textColor=HexColor("#455A64"),
            spaceBefore=3,
            spaceAfter=2,
            keepWithNext=True,
        ),
        "center": style("center", fontSize=9.5, leading=14, alignment=TA_CENTER),
        "bullet": style("bullet", fontSize=9.5, leading=14.5, leftIndent=11, bulletIndent=2),
        "tbl_h": style("tbl_h", fontName="CNB", fontSize=8.5, leading=12, textColor=white),
        "tbl_c": style("tbl_c", fontSize=8.5, leading=12.5),
        "tbl_b": style("tbl_b", fontName="CNB", fontSize=8.5, leading=12.5),
        "tbl_bullet": style(
            "tbl_bullet", fontSize=8.5, leading=12.5, leftIndent=8, bulletIndent=0, spaceAfter=2
        ),
        "card_label": style(
            "card_label", fontName="CNB", fontSize=8.5, leading=12.5, textColor=HexColor("#455A64")
        ),
        "card_value": style("card_value", fontSize=9, leading=13),
    }


class ScoreBadge(Flowable):
    """综合评分徽章：左侧色条标示评级，中部大号分数，底部评级标签。"""

    def __init__(self, score, light, grade, width=56 * mm, height=42 * mm):
        Flowable.__init__(self)
        self.score = score
        self.light = light
        self.grade = grade
        self.width = width
        self.height = height

    def wrap(self, aw, ah):
        return self.width, self.height

    def draw(self):
        c = self.canv
        w, h = self.width, self.height
        fg = HexColor(LIGHT_COLOR.get(self.light, "#37474F"))
        c.setFillColor(HexColor(LIGHT_BG.get(self.light, "#F1F4F2")))
        c.roundRect(0, 0, w, h, 3, fill=1, stroke=0)
        c.setFillColor(fg)
        c.rect(0, 0, 2.2 * mm, h, fill=1, stroke=0)
        cx = w / 2 + 1.1 * mm
        c.setFillColor(HexColor("#455A64"))
        c.setFont("CN", 8.5)
        c.drawCentredString(cx, h - 7.5 * mm, "综合评分（满分 100）")
        c.setFillColor(fg)
        c.setFont("CNB", 30)
        c.drawCentredString(cx, h / 2 - 3.5 * mm, f"{self.score}")
        label = f"{self.grade or ''} · {LIGHT_WORD.get(self.light, self.light or '')}".strip(" ·")
        c.setFont("CN", 8.5)
        pill_w = pdfmetrics.stringWidth(label, "CN", 8.5) + 6 * mm
        c.roundRect(cx - pill_w / 2, 3.5 * mm, pill_w, 6 * mm, 3 * mm, fill=1, stroke=0)
        c.setFillColor(white)
        c.drawCentredString(cx, 5.6 * mm, label)


class AiReferenceCard(Flowable):
    """Secondary AI reference score — clearly not admission / program score."""

    def __init__(
        self,
        score,
        light,
        grade,
        disclaimer="AI参考分 · 不可作为准入结论",
        width=CONTENT_W,
        height=13 * mm,
    ):
        Flowable.__init__(self)
        self.score = score
        self.light = light
        self.grade = grade
        self.disclaimer = disclaimer
        self.width = width
        self.height = height

    def wrap(self, aw, ah):
        return self.width, self.height

    def draw(self):
        c = self.canv
        c.setFillColor(HexColor("#F4F6F8"))
        c.setStrokeColor(HexColor("#90A4AE"))
        c.setLineWidth(0.6)
        c.setDash(2, 2)
        c.roundRect(0.3, 0.3, self.width - 0.6, self.height - 0.6, 3, fill=1, stroke=1)
        c.setDash()
        mid = self.height / 2 - 1.2 * mm
        c.setFillColor(HexColor("#455A64"))
        c.setFont("CN", 8.5)
        c.drawString(5 * mm, mid, "AI 参考分")
        c.setFillColor(HexColor(LIGHT_COLOR.get(self.light, "#445566")))
        c.setFont("CNB", 15)
        c.drawString(23 * mm, mid - 0.6 * mm, f"{self.score}")
        grade = f"{self.grade} · " if self.grade else ""
        c.setFillColor(HexColor("#455A64"))
        c.setFont("CN", 8.5)
        c.drawString(41 * mm, mid, f"{grade}{LIGHT_WORD.get(self.light, self.light or '')}")
        c.setFillColor(HexColor("#8A4B08"))
        c.setFont("CN", 8)
        c.drawRightString(self.width - 5 * mm, mid, self.disclaimer)


def _esc(text: Any) -> str:
    s = "" if text is None else str(text)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ai_text(value: Any, fallback: str = AI_FAIL) -> str:
    if value is None:
        return fallback
    if isinstance(value, list):
        items = [str(x).strip() for x in value if str(x).strip()]
        return "；".join(items) if items else fallback
    s = str(value).strip()
    return s if s else fallback


def _ai_failed(ai: dict[str, Any] | None) -> bool:
    if not ai:
        return True
    if ai.get("error") in ("missing_api_key", "empty_response", "unparseable_response"):
        return True
    if ai.get("error") and not ai.get("llm_configured"):
        return True
    if ai.get("error") and isinstance(ai.get("error"), str):
        # HTTP / timeout soft-fail still has llm_configured True
        if ai.get("llm_configured") and (
            not (ai.get("overall") or {}).get("strengths")
            and AI_FAIL in str((ai.get("overall") or {}).get("evaluation") or "")
        ):
            return True
    return False


def _fmt(value: Any, unit: str = "", default: str = "—") -> str:
    if value is None or value == "":
        return default
    return f"{value}{(' ' + unit) if unit else ''}"


class AssessmentDocTemplate(SimpleDocTemplate):
    """通过多轮排版回填真实目录页码，避免篇幅变化后目录失准。"""

    def afterFlowable(self, flowable):
        title = getattr(flowable, "chapter_title", None)
        if title:
            key = "chapter-" + str(
                next(
                    (i for i, row in enumerate(TOC_ENTRIES) if row[0] == title),
                    len(TOC_ENTRIES),
                )
            )
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(title, key, level=0)
            blurb = next(
                (row[1] for row in TOC_ENTRIES if row[0] == title),
                "逐题答案、红线排查与来源",
            )
            label = (
                f"{_esc(title)}<br/><font size='8.5' color='{MUTED}'>{_esc(blurb)}</font>"
            )
            self.notify("TOCEntry", (0, label, self.page, key))


def render_pdf(
    out_path: Path | str,
    field: dict[str, Any],
    scorecard: dict[str, Any],
    rs: dict[str, Any],
    risk: dict[str, Any],
    soil: dict[str, Any],
    weather_summary: dict[str, Any],
    chart_paths: dict[str, Path] | None = None,
    title_suffix: str = "乡合农服",
    flood_evidence: dict[str, Any] | None = None,
    analysis: dict[str, Any] | None = None,
    ai: dict[str, Any] | None = None,
    site_admission: dict[str, Any] | None = None,
) -> Path:
    """完整渲染选地报告，按章节自然分页，不通过删减正文限制页数。"""
    _register_fonts()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    chart_paths = chart_paths or {}
    analysis = analysis or {}
    ai = ai or {}
    site_admission = site_admission if isinstance(site_admission, dict) else None
    weather_summary = weather_summary or {}
    soil = soil or {}
    rs = rs or {}
    risk = risk or {}

    ov = scorecard["overall"]
    dims = {d["key"]: d for d in scorecard.get("dimensions") or []}
    styles = _styles()

    area_ha = float(field.get("area_ha") or 0)
    area_mu = round(area_ha * 15, 1)
    now = datetime.now(CST)
    now_str = now.strftime("%Y年%m月%d日 %H:%M")
    field_name = field.get("name") or "地块"
    crop_label = field.get("crop_label") or (
        crop_name_zh(field.get("crop_type")) if field.get("crop_type") else "作物"
    )
    land_id = str(field.get("land_id") or field.get("id") or "")
    report_no = (
        f"LA-{now:%Y%m%d}-{land_id.replace('-', '')[:8].upper()}" if land_id else "—"
    )
    ai_fail = _ai_failed(ai)
    counters = {"fig": 0, "tbl": 0}

    def cell(text, style="tbl_c"):
        return Paragraph(_esc(text), styles[style])

    def p(text, style="body"):
        return Paragraph(text, styles[style])

    def hr():
        return HRFlowable(
            width="100%", thickness=0.8, color=HexColor(PRIMARY), spaceBefore=1, spaceAfter=5
        )

    def section_title(ordinal_title: str):
        """Major chapter heading with Chinese ordinal, e.g. 二、地块基础画像."""
        heading = Paragraph(_esc(ordinal_title), styles["h1"])
        heading.chapter_title = ordinal_title
        return heading

    def chapter(title: str, *, new_page: bool = False) -> None:
        # 剩余版面不足时才换页，短章节接续排版，避免大面积空白页。
        story.append(PageBreak() if new_page else CondPageBreak(75 * mm))
        story.append(section_title(title))
        story.append(hr())

    def sub_title(text: str):
        return Paragraph(_esc(text), styles["h2"])

    def table_caption(text: str):
        counters["tbl"] += 1
        return Paragraph(f"表 {counters['tbl']}　{_esc(text)}", styles["tcaption"])

    def _grid_style(n_rows: int, header: bool = True) -> list:
        cmds = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ("LINEABOVE", (0, 0), (-1, 0), 0.9, HexColor(PRIMARY)),
            ("LINEBELOW", (0, -1), (-1, -1), 0.9, HexColor(PRIMARY)),
        ]
        if header:
            cmds += [
                ("BACKGROUND", (0, 0), (-1, 0), HexColor(PRIMARY)),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, HexColor(ZEBRA)]),
            ]
            if n_rows > 1:
                cmds.append(("LINEBELOW", (0, 1), (-1, -2), 0.3, HexColor(RULE)))
        else:
            cmds.append(("LINEBELOW", (0, 0), (-1, -2), 0.3, HexColor(RULE)))
        return cmds

    def make_table(data, col_widths, caption: str | None = None):
        # 统一内容宽度；跨页重复表头并允许超长单元格拆分，不裁掉正文。
        total = sum(col_widths)
        t = Table(
            data,
            colWidths=[w / total * CONTENT_W for w in col_widths],
            repeatRows=1,
            splitInRow=1,
            hAlign="LEFT",
        )
        t.setStyle(TableStyle(_grid_style(len(data))))
        if caption:
            return [table_caption(caption), t, Spacer(1, 2 * mm)]
        return [t, Spacer(1, 2 * mm)]

    def kv_card(title: str, rows: list[tuple[str, str]], width=CONTENT_W):
        """Structured key/value block with a titled header row."""
        safe_rows = rows or [("—", "—")]
        t = Table(
            [[Paragraph(_esc(title), styles["tbl_h"]), ""]]
            + [
                [
                    Paragraph(_esc(k), styles["card_label"]),
                    Paragraph(_esc(v if v not in (None, "") else "—"), styles["card_value"]),
                ]
                for k, v in safe_rows
            ],
            colWidths=[36 * mm, width - 36 * mm],
            repeatRows=1,
            splitInRow=1,
            hAlign="LEFT",
        )
        t.setStyle(
            TableStyle(
                [("SPAN", (0, 0), (-1, 0))]
                + _grid_style(len(safe_rows) + 1)
                + [("BACKGROUND", (0, 1), (0, -1), HexColor("#EDF3EF"))]
            )
        )
        # 普通卡片整体换页；极长卡片仍可由内部表格拆分，避免丢失详细依据。
        return KeepTogether([t, Spacer(1, 3 * mm)])

    def bullets(items: list[Any], empty: str = AI_FAIL):
        cleaned = [str(x).strip() for x in (items or []) if str(x).strip()]
        if not cleaned:
            return [p(f"<font color='#8A4B08'>{_esc(empty)}</font>", "small")]
        return [Paragraph(_esc(it), styles["bullet"], bulletText="•") for it in cleaned]

    def ai_block(title: str, body: str):
        text = (body or "").strip() or AI_FAIL
        if ai_fail and AI_FAIL not in text:
            text = f"{AI_FAIL}：{text}" if text else AI_FAIL
        head = Paragraph(
            f"<b>{_esc(title)}</b>　<font size='7' color='{MUTED}'>AI 辅助解读</font>",
            styles["h2"],
        )
        box = Table(
            [[head], [Paragraph(_esc(text), styles["body"])]],
            colWidths=[CONTENT_W],
            splitInRow=1,
            hAlign="LEFT",
        )
        box.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), HexColor("#F7FAF8")),
                    ("LINEBEFORE", (0, 0), (0, -1), 2.2, HexColor("#7FA88F")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        box.spaceAfter = 3 * mm
        return box

    def note(text: str):
        return p(f"注：{text}", "small")

    def img(name, w=165 * mm, ratio=0.34, caption=None):
        path = chart_paths.get(name)
        if not (path and Path(path).exists()):
            return []
        # 按原图宽高比缩放，ratio 仅限制占用高度，避免图形被压扁。
        from reportlab.lib.utils import ImageReader

        iw, ih = ImageReader(str(path)).getSize()
        scale = min(w / iw, w * ratio / ih)
        picture = Image(str(path), width=iw * scale, height=ih * scale)
        picture.hAlign = "CENTER"
        block: list = [picture]
        if caption:
            counters["fig"] += 1
            block.append(Paragraph(f"图 {counters['fig']}　{_esc(caption)}", styles["caption"]))
        block.append(Spacer(1, 1.5 * mm))
        return [KeepTogether(block)]

    def on_page(c, doc):
        c.saveState()
        left, right, top = 21 * mm, A4[0] - 21 * mm, A4[1]
        if doc.page == 1:
            c.setFillColor(HexColor(PRIMARY))
            c.rect(0, top - 5 * mm, A4[0], 5 * mm, fill=1, stroke=0)
        else:
            c.setFont("CN", 7.5)
            c.setFillColor(HexColor(MUTED))
            c.drawString(left, top - 10 * mm, f"地块遥感选地评估报告 · {field_name}")
            c.drawRightString(right, top - 10 * mm, f"报告编号 {report_no}")
            c.setStrokeColor(HexColor(RULE))
            c.setLineWidth(0.5)
            c.line(left, top - 11.5 * mm, right, top - 11.5 * mm)
        c.setStrokeColor(HexColor(RULE))
        c.setLineWidth(0.5)
        c.line(left, 12 * mm, right, 12 * mm)
        c.setFont("CN", 7.5)
        c.setFillColor(HexColor(MUTED))
        c.drawString(
            left, 7.5 * mm, f"{title_suffix} · 评分由程序计算，文字解读由 AI 辅助生成，仅供参考"
        )
        c.drawRightString(right, 7.5 * mm, f"第 {doc.page} 页")
        c.restoreState()

    overall_ai = ai.get("overall") or {}
    portrait_ai = ai.get("portrait") or {}
    score_ai = ai.get("score_explain") or {}
    rs_ai = ai.get("rs_growth") or {}
    spatial_ai = ai.get("spatial") or {}
    soil_ai = ai.get("soil") or {}
    climate_ai = ai.get("climate") or {}
    mgmt_ai = ai.get("management") or {}
    yield_ai = ai.get("yield_potential") or {}
    biz_ai = ai.get("business") or {}

    story: list = []

    # ── 封面摘要 + 一、综合评价 ──
    story.append(Spacer(1, 4 * mm))
    story.append(p(f"{_esc(title_suffix)} · 农业遥感评估", "cover_brand"))
    story.append(Spacer(1, 2 * mm))
    story.append(p("地块遥感选地评估报告", "cover_title"))
    story.append(
        p(
            f"{_esc(field_name)} · 拟种{_esc(crop_label)} · 约 {area_mu} 亩",
            "cover_sub",
        )
    )
    story.append(Spacer(1, 3 * mm))
    meta_rows = [
        ("地块名称", field_name, "拟种作物", crop_label),
        ("所在位置", field.get("location") or "—", "地块面积", f"{area_mu} 亩（{area_ha} 公顷）"),
        ("边界来源", field.get("boundary") or "—", "数据时段", risk.get("period") or "—"),
        ("报告编号", report_no, "生成时间", now_str),
    ]
    mt = Table(
        [
            [cell(a, "card_label"), cell(b, "card_value"), cell(c_, "card_label"), cell(d, "card_value")]
            for a, b, c_, d in meta_rows
        ],
        colWidths=[22 * mm, 62 * mm, 22 * mm, 62 * mm],
        hAlign="LEFT",
    )
    mt.setStyle(TableStyle(_grid_style(len(meta_rows), header=False)))
    story.append(mt)
    story.append(Spacer(1, 5 * mm))

    story.append(section_title("一、综合评价"))
    story.append(hr())
    badge = ScoreBadge(ov["score"], ov["light"], ov["grade"])
    if dims:
        dim_rows = [[cell("评估维度", "tbl_h"), cell("权重", "tbl_h"), cell("得分", "tbl_h"), cell("评级", "tbl_h")]]
        for key in DIM_ORDER:
            d = dims.get(key)
            if not d:
                continue
            light = d.get("light")
            word = LIGHT_WORD.get(light, light or "—")
            dim_rows.append(
                [
                    cell(DIM_TITLE.get(key, d.get("name") or key)),
                    cell(d.get("weight") or "—"),
                    cell(d.get("score", "—"), "tbl_b"),
                    Paragraph(
                        f"<font color='{LIGHT_COLOR.get(light, INK)}'>●</font> {_esc(word)}",
                        styles["tbl_c"],
                    ),
                ]
            )
        dim_table = Table(dim_rows, colWidths=[38 * mm, 20 * mm, 22 * mm, 28 * mm], hAlign="LEFT")
        dim_table.setStyle(TableStyle(_grid_style(len(dim_rows))))
        dash = Table([[badge, dim_table]], colWidths=[60 * mm, 108 * mm], hAlign="LEFT")
        dash.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        story.append(dash)
    else:
        story.append(badge)
    story.append(Spacer(1, 3 * mm))
    story.append(p(f"<b>评估结论：</b>{_esc(ov.get('one_liner') or '—')}", "body"))
    story.append(
        note(
            "综合评分为程序计算结果，由六维指标加权并经数据置信度修正得出；"
            "评级标准：≥70 适宜，55–69 基本适宜，&lt;55 需关注。"
        )
    )
    story.append(Spacer(1, 2 * mm))
    # Dual-track: secondary AI reference score (never replaces program overall)
    ai_ref_score = ai.get("ai_reference_score") if isinstance(ai, dict) else None
    ai_ref_score_f = None
    if ai_ref_score is not None:
        try:
            ai_ref_score_f = float(ai_ref_score)
        except (TypeError, ValueError):
            ai_ref_score_f = None
    if ai_ref_score_f is not None:
        disclaimer = ai.get("ai_reference_disclaimer") or "AI参考分 · 不可作为准入结论"
        story.append(
            AiReferenceCard(
                round(ai_ref_score_f, 1),
                ai.get("ai_reference_light") or ov.get("light"),
                ai.get("ai_reference_grade"),
                disclaimer=disclaimer,
            )
        )
        story.append(Spacer(1, 1.5 * mm))
        rationale = (ai.get("ai_reference_rationale") or "").strip()
        if rationale:
            story.append(p(f"<b>参考依据：</b>{_esc(rationale)}", "small"))
        story.append(
            p(
                f"<font color='#8A4B08'>{_esc(disclaimer)}</font>，"
                "仅供农技研判参考，不参与综合评分。",
                "small",
            )
        )
        story.append(Spacer(1, 2 * mm))
    elif ai_fail:
        story.append(
            p(
                "<font color='#8A4B08'>AI 参考分：缺失（AI 分析失败或未返回）</font>，"
                "程序综合分不受影响。",
                "small",
            )
        )
        story.append(Spacer(1, 2 * mm))
    story.append(ai_block("综合解读", _ai_text(overall_ai.get("evaluation"))))
    summary_cols = (
        ("优势", overall_ai.get("strengths")),
        ("主要风险", overall_ai.get("main_risks")),
        ("核心建议", overall_ai.get("core_advice")),
    )
    summary_body = []
    for _, items in summary_cols:
        cleaned = [str(x).strip() for x in (items or []) if str(x).strip()]
        summary_body.append(
            [Paragraph(_esc(x), styles["tbl_bullet"], bulletText="•") for x in cleaned]
            or [p(f"<font color='#8A4B08'>{AI_FAIL}</font>", "small")]
        )
    summary = Table(
        [[cell(title, "tbl_h") for title, _ in summary_cols], summary_body],
        colWidths=[CONTENT_W / 3] * 3,
        hAlign="LEFT",
    )
    summary.setStyle(
        TableStyle(
            _grid_style(2)
            + [("LINEAFTER", (0, 0), (-2, -1), 0.3, HexColor(RULE)), ("BACKGROUND", (0, 1), (-1, -1), white)]
        )
    )
    story.append(summary)

    # ── 目录 ──
    story.append(PageBreak())
    story.append(p("目录", "h1"))
    story.append(hr())
    story.append(Spacer(1, 2 * mm))
    # 目录由实际分页回填，章节标题、提要和页码均可点击跳转。
    contents = TableOfContents()
    contents.levelStyles = [
        ParagraphStyle(
            "contents",
            fontName="CN",
            fontSize=10.5,
            leading=16,
            textColor=HexColor(INK),
            spaceBefore=9,
            spaceAfter=3,
            leftIndent=0,
            firstLineIndent=0,
        )
    ]
    contents.dotsMinLevel = 0
    story.append(contents)

    # ── 二、地块基础画像 ──
    chapter("二、地块基础画像", new_page=True)
    method = scorecard.get("method") or {}
    windows = (method.get("phenology") or {}).get("windows") or []
    season_txt = (
        "；".join(
            f"{w.get('start_date') or '起点未覆盖'} 至 {w.get('end_date') or '未识别到收获日'}"
            for w in windows
        )
        or "有效观测不足，未能推断生长季"
    )
    story.append(
        kv_card(
            "地块概况",
            [
                ("位置", field.get("location") or "—"),
                ("面积", f"约 {area_mu} 亩（{area_ha} 公顷）"),
                ("拟种作物", crop_label),
                ("观测生长季", season_txt),
                ("数据时段", risk.get("period") or "—"),
            ],
        )
    )
    soil_rows = [
        ("质地", soil_texture_zh(soil.get("dominant_texture")) or "—"),
        ("pH", _fmt(soil.get("avg_ph"))),
        ("排水等级", soil_drainage_zh(soil.get("drainage_class")) or "—"),
        (AWC_LABEL_ZH, _fmt(soil.get("rootzone_awc_mm"), "mm")),
        (
            "渍水风险指数",
            f"{soil.get('waterlogging_risk')}（0–1，越高越易渍水）"
            if soil.get("waterlogging_risk") is not None
            else "—",
        ),
    ]
    npk = soil.get("npk") if isinstance(soil.get("npk"), dict) else None
    if npk:
        soil_rows += [
            ("全氮", _fmt(npk.get("tn_g_kg"), "g/kg")),
            ("有效磷", _fmt(npk.get("ap_mg_kg"), "mg/kg")),
            ("速效钾", _fmt(npk.get("ak_mg_kg"), "mg/kg")),
        ]
    story.append(kv_card("土壤关键指标", soil_rows))
    if site_admission:
        sa = site_admission
        labels = sa.get("key_labels") if isinstance(sa.get("key_labels"), dict) else {}
        sa_rows: list[tuple[str, str]] = [
            ("问卷分组", str(sa.get("group_id") or "—")),
            (
                "现场评分",
                f"{sa.get('score')}（{sa.get('status') or '—'}）"
                if sa.get("score") is not None
                else (sa.get("status") or "—"),
            ),
            ("评估面积", _fmt(sa.get("total_area_mu"), "亩")),
        ]
        for k, label in (
            ("soil_type", "土壤类型"),
            ("land_nature", "土地性质"),
            ("terrain", "地形地势"),
            ("water_source", "水源"),
            ("water_flow", "出水量"),
            ("drainage", "排水"),
            ("power", "电力"),
            ("traffic", "交通"),
        ):
            if labels.get(k):
                sa_rows.append((label, str(labels[k])))
        crops = sa.get("planned_crops") or []
        if crops:
            sa_rows.append(("拟种作物", "、".join(str(c) for c in crops)))
        story.append(kv_card("现场准入问卷（中和农信）", sa_rows))
    wh_plain = analysis.get("weather_history_plain") or ""
    climate_rows = [("历史气候摘要", wh_plain or "—")]
    if weather_summary:
        climate_rows += [
            ("近月热胁迫", _fmt(weather_summary.get("heat_stress_days"), "天")),
            ("近月水分盈亏", _fmt(weather_summary.get("water_deficit_mm"), "mm")),
        ]
    story.append(kv_card("气候基线", climate_rows))
    story.append(ai_block("（一）区域农业特征", _ai_text(portrait_ai.get("regional_ag_traits"))))
    story.append(ai_block("（二）作物适宜性", _ai_text(portrait_ai.get("crop_fit"))))
    story.append(sub_title("（三）限制因素"))
    story += bullets(portrait_ai.get("limits") or [])

    # ── 三、综合评分解释 ──
    chapter("三、综合评分解释")
    radar = img("score_radar.png", w=92 * mm, ratio=0.98, caption="六维评分雷达图")
    if radar:
        story += radar
    else:
        story.append(p("六维分数不完整，雷达图未生成。", "small"))
    legend_rows = [
        [cell("维度", "tbl_h"), cell("权重", "tbl_h"), cell("得分", "tbl_h"), cell("评级", "tbl_h"), cell("判定依据", "tbl_h")]
    ]
    for key in DIM_ORDER:
        d = dims.get(key) or {}
        legend_rows.append(
            [
                cell(DIM_TITLE.get(key, d.get("name") or key), "tbl_b"),
                cell(d.get("weight") or "—"),
                cell(d.get("score", "—"), "tbl_b"),
                cell(LIGHT_WORD.get(d.get("light"), d.get("light") or "—")),
                cell(translate_soil_jargon(d.get("plain") or "—")),
            ]
        )
    story += make_table(legend_rows, [24, 13, 13, 17, 98], caption="六维评分明细")
    story.append(sub_title("1. 优势维度"))
    story += bullets(score_ai.get("high_dims") or [])
    story.append(sub_title("2. 短板维度"))
    story += bullets(score_ai.get("low_dims") or [])
    story.append(sub_title("3. 主要驱动因素"))
    story += bullets(score_ai.get("biggest_drivers") or [])
    story.append(sub_title("4. 改进方向"))
    story += bullets(score_ai.get("how_to_improve") or [])

    # ── 四、遥感长势 ──
    chapter("四、遥感长势")
    season_scenes = (rs.get("counts") or {}).get("season_scenes") or risk.get("n_season_scenes") or "—"
    story.append(
        p(
            f"峰值期 NDVI 均值 <b>{_esc(_fmt(rs.get('peak_ndvi_mean')))}</b>，"
            f"非生长季 NDVI 均值 {_esc(_fmt(rs.get('offseason_ndvi_mean')))}，"
            f"生育期有效场景 {_esc(season_scenes)} 景。评估采用生育期口径，不使用全年均值。",
            "body",
        )
    )
    story.append(Spacer(1, 2 * mm))
    if chart_paths.get("indices_timeseries.png"):
        story += img(
            "indices_timeseries.png",
            w=165 * mm,
            ratio=0.58,
            caption="植被指数（NDVI/EVI）与水分指数（NDWI）时间序列；阴影为观测生长季，虚线为生育期阈值",
        )
    else:
        story += img("ndvi.png", w=165 * mm, ratio=0.34, caption="NDVI 时间序列")
        story += img("evi.png", w=165 * mm, ratio=0.34, caption="EVI 时间序列")
    shares = analysis.get("ndvi_grade_shares") or {}
    if shares.get("n"):
        pct = shares.get("pct") or {}
        story.append(
            p(
                f"生育期 NDVI 等级构成：优 {pct.get('优', 0)}%、良 {pct.get('良', 0)}%、"
                f"中 {pct.get('中', 0)}%、差 {pct.get('差', 0)}%（共 {shares.get('n')} 景）。",
                "body",
            )
        )
        story += img(
            "ndvi_grade_shares.png",
            w=165 * mm,
            ratio=0.2,
            caption=f"生育期 NDVI 等级构成（{shares.get('rule_zh') or ''}）",
        )
    pheno = analysis.get("phenology_stage_summary") or []
    if pheno:
        prow = [
            [cell("物候阶段", "tbl_h"), cell("代表日期", "tbl_h"), cell("NDVI 均值", "tbl_h"), cell("较上阶段", "tbl_h"), cell("等级", "tbl_h")]
        ]
        for stg in pheno:
            mean = stg.get("mean_ndvi")
            grade = "疑似裸地" if stg.get("likely_bare") else (stg.get("grade") or "—")
            prow.append(
                [
                    cell(stg.get("label") or stg.get("key") or "—"),
                    cell(stg.get("date") or "—"),
                    cell(f"{mean:.3f}" if mean is not None else "—"),
                    cell(stg.get("trend_vs_prev") or "—"),
                    cell(grade),
                ]
            )
        story += make_table(prow, [30, 30, 26, 30, 44], caption="物候阶段 NDVI 统计")
    story += img("ndvi_stage_trend.png", w=135 * mm, ratio=0.42, caption="各物候阶段 NDVI 均值与等级阈值")
    em = analysis.get("emergence") or {}
    em_note = em.get("note_zh") or analysis.get("emergence_note")
    if em_note:
        story.append(note(_esc(em_note)))
    story.append(ai_block("物候与长势解读", _ai_text(rs_ai.get("phenology_normality"))))

    story.append(sub_title("1. 异常点解读"))
    events = analysis.get("risk_events_evidence") or risk.get("events") or []
    ai_anoms = rs_ai.get("anomalies") or []
    ev_by_id = {
        str(ev.get("id")): ev for ev in events if isinstance(ev, dict) and ev.get("id")
    }
    if ai_anoms:
        arows = [
            [cell("事件", "tbl_h"), cell("时段 / 阶段", "tbl_h"), cell("异常表现", "tbl_h"), cell("可能成因", "tbl_h"), cell("判断依据", "tbl_h"), cell("置信度", "tbl_h")]
        ]
        for card in ai_anoms:
            if isinstance(card, dict) and card.get("problem"):
                eid = str(card.get("event_id") or "—")
                ev = ev_by_id.get(eid) or {}
                when = " / ".join(x for x in (ev.get("period_full"), ev.get("stage")) if x) or "—"
                arows.append(
                    [
                        cell(eid, "tbl_b"),
                        cell(when),
                        cell(card.get("problem") or "—"),
                        cell(card.get("likely_cause") or "—"),
                        cell(card.get("basis") or "—"),
                        cell(card.get("confidence") or "—"),
                    ]
                )
            elif card:
                arows.append([cell("—"), cell("—"), cell(card), cell("—"), cell("—"), cell("—")])
        story += make_table(arows, [11, 30, 36, 18, 56, 14], caption="异常事件解读（AI 辅助）")
    elif events:
        story.append(p("异常事件的 AI 解读未返回，观测证据见下表。", "small"))
    else:
        story.append(p("生育期内未检出长势偏弱或偏湿聚类事件。", "small"))

    story.append(sub_title("2. 异常事件观测证据"))
    if events:
        erows = [
            [cell("事件", "tbl_h"), cell("时段", "tbl_h"), cell("指数表现", "tbl_h"), cell("同期天气 / 水分", "tbl_h"), cell("物候阶段", "tbl_h")]
        ]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            period = ev.get("period_full") or f"{ev.get('start', '')}～{ev.get('end', '')}"
            erows.append(
                [
                    cell(str(ev.get("id") or "—"), "tbl_b"),
                    cell(str(period)),
                    cell(str(ev.get("performance") or ev.get("type") or "—")),
                    cell(str(ev.get("weather_moisture") or "—")),
                    cell(str(ev.get("stage") or "—")),
                ]
            )
        story += make_table(erows, [11, 30, 54, 44, 26], caption="异常事件观测证据（程序计算）")
        story.append(
            note("指数值为事件窗口内有效场景均值；缺少月尺度气象数据时仅列水分指数或地块级摘要。")
        )
    else:
        story.append(p("无异常事件记录。", "small"))

    story.append(sub_title("3. 成因排序"))
    ranked = rs_ai.get("ranked_causes") or []
    if ranked:
        for item in ranked:
            if isinstance(item, dict):
                line = f"{item.get('rank', '')}. {_esc(item.get('cause') or '')}"
                if item.get("evidence"):
                    line += f" —— 依据：{_esc(item.get('evidence'))}"
                story.append(Paragraph(line, styles["bullet"]))
            else:
                story.append(Paragraph(_esc(item), styles["bullet"], bulletText="•"))
    else:
        story.append(p(f"<font color='#8A4B08'>{AI_FAIL}</font>", "small"))

    # ── 五、空间异常 ──
    chapter("五、空间异常")
    phenology_name = next(
        (
            k
            for k in chart_paths
            if str(k).startswith("ndvi_phenology_") and str(k).endswith(".png")
        ),
        "ndvi_phenology.png" if chart_paths.get("ndvi_phenology.png") else None,
    )
    if phenology_name:
        year = analysis.get("phenology_year")
        story += img(
            phenology_name,
            w=165 * mm,
            ratio=0.38,
            caption=f"{str(year) + ' 年' if year else ''}生长季 NDVI 曲线与物候阶段节点",
        )
    if chart_paths.get("ndvi_stages_panel.png"):
        story += img(
            "ndvi_stages_panel.png",
            w=150 * mm,
            ratio=0.9,
            caption="各物候阶段 NDVI 空间分布（按阶段先后排列，CV 为像元变异系数）",
        )
    else:
        story.append(note("暂无可用的像元或真彩影像，仅展示物候曲线；影像补齐后自动生成空间分布图。"))
    story.append(sub_title("1. 需关注区域"))
    zones = spatial_ai.get("watch_zones") or []
    why = spatial_ai.get("why") or []
    if zones:
        story += bullets(zones)
    elif spatial_ai.get("no_hotspot") or why:
        # Empty watch_zones with why/no_hotspot is a valid AI answer, not a fail
        story.append(p("未发现显著空间异质斑块，异常变化表现为全田同步。", "body"))
    else:
        story += bullets([], empty=AI_FAIL)
    story.append(sub_title("2. 成因判断"))
    story += bullets(why)
    story.append(
        ai_block(
            "时间连续性说明",
            _ai_text(
                spatial_ai.get("temporal_caveat"),
                "单景不足以定论，需结合多时相连续观测与田间核实。",
            ),
        )
    )

    # ── 六、土壤 ──
    chapter("六、土壤")
    soil_plain = translate_soil_jargon(analysis.get("soil_analysis_plain") or "")
    if soil_plain:
        story.append(p(f"<b>土壤概况：</b>{_esc(soil_plain)}", "body"))
    else:
        story.append(
            p(
                f"质地 {_esc(soil_texture_zh(soil.get('dominant_texture')) or '—')}；"
                f"pH {_esc(_fmt(soil.get('avg_ph')))}；"
                f"排水 {_esc(soil_drainage_zh(soil.get('drainage_class')) or '—')}；"
                f"{_esc(AWC_LABEL_ZH)} {_esc(_fmt(soil.get('rootzone_awc_mm')))}；"
                f"渍水风险指数 {_esc(_fmt(soil.get('waterlogging_risk')))}。",
                "body",
            )
        )
    if npk:
        sqi = npk.get("sqi_rating") or npk.get("sqi_score")
        story.append(
            p(
                "<b>养分：</b>"
                f"全氮 {_fmt(npk.get('tn_g_kg'), 'g/kg')}；"
                f"碱解氮 {_fmt(npk.get('an_mg_kg'), 'mg/kg')}；"
                f"有效磷 {_fmt(npk.get('ap_mg_kg'), 'mg/kg')}；"
                f"速效钾 {_fmt(npk.get('ak_mg_kg'), 'mg/kg')}"
                + (f"；有机质 {npk.get('som_g_kg')} g/kg" if npk.get("som_g_kg") is not None else "")
                + (f"；综合地力 {_esc(sqi)}" if sqi is not None else "")
                + "。",
                "body",
            )
        )
    story.append(Spacer(1, 2 * mm))
    srows = [[cell("指标", "tbl_h"), cell("田间影响", "tbl_h"), cell("管理方向", "tbl_h")]]
    soil_ai_rows = soil_ai.get("indicators_to_farm") or []
    for r in soil_ai_rows:
        if isinstance(r, dict):
            srows.append(
                [
                    cell(translate_soil_jargon(r.get("indicator") or "—"), "tbl_b"),
                    cell(translate_soil_jargon(r.get("farm_impact") or "—")),
                    cell(translate_soil_jargon(r.get("management") or "—")),
                ]
            )
        else:
            srows.append([cell(str(r)), cell("—"), cell("—")])
    if not soil_ai_rows:
        srows.append([cell(AI_FAIL), cell(AI_FAIL), cell(AI_FAIL)])
    story += make_table(srows, [40, 62, 63], caption="土壤指标与田间影响（AI 辅助）")
    story.append(note("未建立农艺施肥模型，不给出具体施肥量（kg/亩）。"))

    # ── 七、气候风险 ──
    chapter("七、气候风险")
    if wh_plain:
        story.append(p(f"<b>历史气候：</b>{_esc(wh_plain)}", "body"))
    story.append(
        p(
            f"近月热胁迫 {_esc(weather_summary.get('heat_stress_days', '—'))} 天；"
            f"水分盈亏 {_esc(weather_summary.get('water_deficit_mm', '—'))} mm；"
            f"遥感渍涝信号 {_esc(rs.get('rs_flood_level') or '—')}；"
            f"遥感干旱信号 {_esc(rs.get('rs_drought_level') or '—')}。",
            "body",
        )
    )
    story += img(
        "weather_history.png",
        w=165 * mm,
        ratio=0.4,
        caption="生育期逐月降水量、参考蒸散与月均气温",
    )
    if flood_evidence and int(flood_evidence.get("absolute_open_water_scenes") or 0) > 0:
        story.append(sub_title("1. 明水面观测证据"))
        story.append(
            p(
                f"生育期内卫星观测到明水面 <b>{flood_evidence.get('absolute_open_water_scenes')}</b> 景。"
                f"{_esc(flood_evidence.get('analysis') or '')}",
                "body",
            )
        )
        story += img("flood_rgb_collage.png", w=160 * mm, ratio=0.5, caption="明水面场景真彩色影像")
        story += img("flood_precip_top.png", w=110 * mm, ratio=0.4, caption="最湿场景前 15 日逐日降水（按国标降水等级着色）")
    story.append(sub_title("2. 风险提示"))
    story += bullets(climate_ai.get("risk_present") or [])
    story.append(sub_title("3. 已发生灾害（需硬证据）"))
    disasters = climate_ai.get("disaster_occurred") or []
    if disasters:
        story += bullets(disasters)
    else:
        story.append(p("无硬证据支持的已发生灾害。", "small"))
    if climate_ai.get("notes"):
        story.append(ai_block("气候补充说明", _ai_text(climate_ai.get("notes"))))

    # ── 八、种植管理建议 ──
    chapter("八、种植管理建议")
    if site_admission:
        lbl = site_admission.get("key_labels") or {}
        bits = [
            f"{name}：{lbl[k]}"
            for k, name in (("drainage", "排水"), ("water_source", "水源"), ("soil_type", "土类"))
            if lbl.get(k)
        ]
        if bits:
            story.append(p("<b>现场条件：</b>" + _esc("；".join(bits)) + "。", "body"))
    story.append(ai_block("品种方向", _ai_text(mgmt_ai.get("variety_direction"))))
    story.append(sub_title("1. 播种与田间管理"))
    story += bullets(mgmt_ai.get("planting_focus") or [])
    story.append(sub_title("2. 水肥管理"))
    story += bullets(mgmt_ai.get("water_fertility_watch") or [])
    story.append(sub_title("3. 巡田要点"))
    story += bullets(mgmt_ai.get("scouting") or [])
    story.append(note("播期、施肥量与灌溉量需结合当地农技部门意见与田间实测确定。"))

    # ── 九、产量潜力 ──
    chapter("九、产量潜力")
    level = yield_ai.get("level")
    if level in ("高", "中", "低"):
        story.append(p(f"<b>潜力等级（相对）：</b>{level}", "body"))
    else:
        story.append(p("<b>潜力等级：</b>数据不足，暂不评定。", "body"))
    story.append(ai_block("依据说明", _ai_text(yield_ai.get("rationale"))))
    story.append(note("未建立实测产量模型，仅给出相对等级，不给出亩产数值。"))

    # ── 十、经营分析 ──
    chapter("十、经营分析")
    if not bool(biz_ai.get("available")):
        story.append(p("当前未接入价格、成本与产量模型，不提供收益测算。", "body"))
        story.append(ai_block("说明", _ai_text(biz_ai.get("note"), "数据不足")))
    else:
        story.append(ai_block("经营解读", _ai_text(biz_ai.get("note"))))
    gaps = ai.get("evidence_gaps") or []
    if gaps:
        story.append(sub_title("1. 待补充数据"))
        story += bullets(gaps, empty="—")

    weights = "、".join(
        f"{DIM_TITLE[k]} {dims[k].get('weight')}" for k in DIM_ORDER if k in dims and dims[k].get("weight")
    )
    confidence = (scorecard.get("confidence") or {}).get("plain") or "—"
    method_block: list = [
        sub_title("2. 数据来源与方法"),
        p(
            "<b>评分方法：</b>综合评分 = 六维加权分 ×（0.85 + 0.15 × 数据置信度 / 100）"
            + (f"；权重：{_esc(weights)}" if weights else "")
            + f"。<b>数据置信度：</b>{_esc(confidence)}",
            "small",
        ),
    ]
    src_rows = [
        [cell("数据类别", "tbl_h"), cell("来源", "tbl_h"), cell("用途说明", "tbl_h")],
        [cell("卫星遥感", "tbl_b"), cell("Sentinel-2 L2A 多光谱影像（10 m）"), cell("按场景质量评分筛选有效观测，计算地块级植被与水分指数")],
        [cell("气象", "tbl_b"), cell("Open-Meteo 历史及近期气象"), cell("逐日降水、气温、参考蒸散，统计生育期水热条件")],
        [cell("土壤", "tbl_b"), cell("SoilGrids 等土壤数据库"), cell("质地、pH、排水、根系层有效持水量及养分")],
    ]
    if site_admission:
        src_rows.append([cell("现场问卷", "tbl_b"), cell(site_admission.get("source") or "现场准入问卷"), cell("现场条件与红线排查，用于交叉核验")])
    method_block += make_table(src_rows, [22, 58, 85], caption="数据来源")
    idx_rows = [[cell("指数", "tbl_h"), cell("计算公式", "tbl_h"), cell("指示意义", "tbl_h")]]
    for name, formula, meaning in SATELLITE_INDICES:
        idx_rows.append([cell(name, "tbl_b"), cell(formula), cell(meaning)])
    method_block += make_table(idx_rows, [16, 74, 75], caption="遥感指数定义")
    story.append(KeepTogether(method_block))
    if ai.get("error"):
        story.append(
            note(f"AI 解读部分生成异常（{_esc(ai.get('error'))}），程序计算的评分、图表与指标不受影响。")
        )

    if site_admission:
        # 问卷摘要保留在画像章节；逐题证据另页完整列出，方便追溯AI引用依据。
        story.append(PageBreak())
        story.append(section_title("附录、现场问卷明细"))
        story.append(hr())
        story.append(
            p(
                "以下为现场问卷填报资料，作为各章节解读的输入；与遥感、气象、土壤数据存在差异时，应结合来源与采集时间核查。",
                "body",
            )
        )
        story.append(
            kv_card(
                "问卷来源",
                [
                    ("来源", site_admission.get("source") or "未注明"),
                    ("获取时间", site_admission.get("fetched_at") or "未注明"),
                ],
            )
        )
        items = {
            str(item.get("id")): item
            for dimension in site_admission.get("dimensions") or []
            if isinstance(dimension, dict)
            for item in dimension.get("items") or []
            if isinstance(item, dict) and item.get("id") is not None
        }
        answers = dict(site_admission.get("item_answers") or {})
        for key, item in items.items():
            if key not in answers:
                answers[key] = item.get("option_label") or item.get("option_key")
        survey_rows = [[cell("题目", "tbl_h"), cell("现场回答", "tbl_h")]]
        for key, value in answers.items():
            item = items.get(str(key)) or {}
            label = item.get("name") or key
            # 只有原始选项与该题评分选项一致时才使用中文选项名，避免覆盖矛盾回答。
            if item.get("option_label") and value == item.get("option_key"):
                value = item["option_label"]
            if isinstance(value, (dict, list, bool)):
                value = json.dumps(value, ensure_ascii=False)
            survey_rows.append([cell(label), cell(value if value is not None else "未回答")])
        if len(survey_rows) > 1:
            story += make_table(survey_rows, [55, 110], caption="逐题回答")
        else:
            story.append(p("暂无逐题答案，问卷摘要见地块基础画像。", "small"))
        red_lines = site_admission.get("red_line_answers") or {}
        if red_lines:
            story.append(sub_title("红线排查（原始回答）"))
            rows = [[cell("排查项", "tbl_h"), cell("填报内容", "tbl_h")]]
            for key, value in red_lines.items():
                rows.append([cell(key), cell(json.dumps(value, ensure_ascii=False))])
            story += make_table(rows, [55, 110])

    doc = AssessmentDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=21 * mm,
        rightMargin=21 * mm,
        topMargin=17 * mm,
        bottomMargin=17 * mm,
        title=f"{field_name}地块遥感选地评估报告",
        author="乡合农服",
    )
    doc.multiBuild(story, onFirstPage=on_page, onLaterPages=on_page)
    return out_path
