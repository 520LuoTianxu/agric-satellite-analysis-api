# -*- coding: utf-8 -*-
"""Reportlab PDF renderer for 选地体检（≤10 页，facts + AI）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
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

from app.reports.land_assessment.paths import FONT_PATH
from app.reports.land_assessment.soil_labels import (
    AWC_LABEL_ZH,
    soil_drainage_zh,
    soil_texture_zh,
    translate_soil_jargon,
)

LIGHT_WORD = {"绿": "不错", "黄": "一般", "红": "要盯紧"}
LIGHT_COLOR = {"绿": "#1b7a3d", "黄": "#c48a00", "红": "#c0392b"}
LIGHT_BG = {"绿": "#e8f6ee", "黄": "#fff7e0", "红": "#fdecea"}
AI_FAIL = "AI 分析失败"

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

# Major chapters (cover is 一；目录 is unnumbered thin page)
TOC_ENTRIES = [
    ("一、AI选地综合评价", "综合评分与总体解读"),
    ("二、地块基础画像", "位置·土壤·气候与适配"),
    ("三、综合评分解释", "六维雷达与高低维度解读"),
    ("四、遥感长势", "绿度曲线、阶段与异常证据"),
    ("五、空间异常", "需关注区域与时间连续性"),
    ("六、土壤", "指标到田间影响"),
    ("七、气候风险", "历史气候与涝旱证据"),
    ("八、种植管理建议", "品种·播种·水肥·巡田"),
    ("九、产量潜力", "相对等级（无亩产数字）"),
    ("十、经营分析", "有模型才给金额"),
]


def _register_fonts() -> None:
    if "CN" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("CN", str(FONT_PATH)))
        pdfmetrics.registerFont(TTFont("CNB", str(FONT_PATH)))


def _styles() -> dict[str, ParagraphStyle]:
    return {
        "cover_title": ParagraphStyle(
            "cover_title",
            fontName="CNB",
            fontSize=20,
            leading=28,
            alignment=TA_CENTER,
            textColor=HexColor("#143d2b"),
        ),
        "cover_sub": ParagraphStyle(
            "cover_sub",
            fontName="CN",
            fontSize=10,
            leading=14,
            alignment=TA_CENTER,
            textColor=HexColor("#5a6a60"),
        ),
        "h1": ParagraphStyle(
            "h1",
            fontName="CNB",
            fontSize=13,
            leading=20,
            textColor=HexColor("#143d2b"),
            spaceBefore=4,
            spaceAfter=4,
        ),
        "h2": ParagraphStyle(
            "h2",
            fontName="CNB",
            fontSize=11,
            leading=16,
            textColor=HexColor("#1f4d38"),
            spaceBefore=4,
            spaceAfter=2,
        ),
        "body": ParagraphStyle(
            "body",
            fontName="CN",
            fontSize=9.5,
            leading=14.5,
            alignment=TA_JUSTIFY,
            textColor=HexColor("#222"),
        ),
        "small": ParagraphStyle(
            "small", fontName="CN", fontSize=8, leading=12, textColor=HexColor("#666")
        ),
        "center": ParagraphStyle(
            "center",
            fontName="CN",
            fontSize=10,
            leading=14,
            alignment=TA_CENTER,
            textColor=HexColor("#333"),
        ),
        "badge": ParagraphStyle(
            "badge",
            fontName="CNB",
            fontSize=11,
            leading=14,
            alignment=TA_CENTER,
            textColor=white,
        ),
        "bullet": ParagraphStyle(
            "bullet",
            fontName="CN",
            fontSize=9,
            leading=13.5,
            textColor=HexColor("#222"),
            leftIndent=6,
        ),
        "ai_note": ParagraphStyle(
            "ai_note",
            fontName="CN",
            fontSize=9,
            leading=13.5,
            textColor=HexColor("#7a3a00"),
            backColor=HexColor("#fff7e0"),
        ),
        "tbl_h": ParagraphStyle(
            "tbl_h", fontName="CNB", fontSize=8.5, leading=11, textColor=white
        ),
        "tbl_c": ParagraphStyle(
            "tbl_c", fontName="CN", fontSize=8, leading=11.5, textColor=HexColor("#222")
        ),
        "tbl_b": ParagraphStyle(
            "tbl_b",
            fontName="CNB",
            fontSize=8,
            leading=11.5,
            textColor=HexColor("#222"),
        ),
        "toc": ParagraphStyle(
            "toc",
            fontName="CN",
            fontSize=10,
            leading=16,
            textColor=HexColor("#222"),
            leftIndent=4,
        ),
        "card_label": ParagraphStyle(
            "card_label",
            fontName="CNB",
            fontSize=8.5,
            leading=12,
            textColor=HexColor("#1f4d38"),
        ),
        "card_value": ParagraphStyle(
            "card_value",
            fontName="CN",
            fontSize=9,
            leading=13,
            textColor=HexColor("#222"),
        ),
    }


class ScoreBadge(Flowable):
    def __init__(self, score, light, grade, width=170 * mm, height=32 * mm):
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
        bg = HexColor(LIGHT_BG.get(self.light, "#eee"))
        fg = HexColor(LIGHT_COLOR.get(self.light, "#333"))
        c.setFillColor(bg)
        c.roundRect(0, 0, self.width, self.height, 8, fill=1, stroke=0)
        c.setStrokeColor(fg)
        c.setLineWidth(2)
        c.roundRect(1, 1, self.width - 2, self.height - 2, 8, fill=0, stroke=1)
        for i, (col, on) in enumerate(
            [
                ("#c0392b", self.light == "红"),
                ("#c48a00", self.light == "黄"),
                ("#1b7a3d", self.light == "绿"),
            ]
        ):
            y = self.height - 8 * mm - i * 7.5 * mm
            c.setFillColor(HexColor(col) if on else HexColor("#cfd8cf"))
            c.circle(10 * mm, y, 3.0 * mm if on else 2.4 * mm, fill=1, stroke=0)
        c.setFillColor(fg)
        c.setFont("CNB", 30)
        c.drawCentredString(
            self.width / 2 + 4 * mm, self.height / 2 + 1 * mm, f"{self.score}"
        )
        c.setFont("CN", 10)
        c.setFillColor(HexColor("#444"))
        c.drawCentredString(
            self.width / 2 + 4 * mm,
            6 * mm,
            f"{self.grade} · {LIGHT_WORD.get(self.light, self.light)}",
        )


def _esc(text: Any) -> str:
    s = str(text or "")
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


def render_pdf(
    out_path: Path | str,
    field: dict[str, Any],
    scorecard: dict[str, Any],
    rs: dict[str, Any],
    risk: dict[str, Any],
    soil: dict[str, Any],
    weather_summary: dict[str, Any],
    chart_paths: dict[str, Path] | None = None,
    title_suffix: str = "OpenFarm",
    flood_evidence: dict[str, Any] | None = None,
    analysis: dict[str, Any] | None = None,
    ai: dict[str, Any] | None = None,
) -> Path:
    """Render ≤10-page land-assessment PDF from program facts + AI JSON."""
    _register_fonts()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    chart_paths = chart_paths or {}
    analysis = analysis or {}
    ai = ai or {}

    ov = scorecard["overall"]
    dims = {d["key"]: d for d in scorecard["dimensions"]}
    styles = _styles()

    area_ha = float(field.get("area_ha") or 0)
    area_mu = round(area_ha * 15, 1)
    now_str = datetime.now(CST).strftime("%Y年%m月%d日 %H:%M")
    crop_label = field.get("crop_label") or field.get("crop_type") or "作物"
    ai_fail = _ai_failed(ai)

    def cell(text, style="tbl_c"):
        return Paragraph(_esc(text), styles[style])

    def section_title(ordinal_title: str):
        """Major chapter heading with Chinese ordinal, e.g. 二、地块基础画像."""
        return Paragraph(_esc(ordinal_title), styles["h1"])

    def sub_title(text: str, level: str = "1"):
        """Subhead: 1. / （一） style."""
        return Paragraph(f"<b>{_esc(text)}</b>", styles["h2"])

    def kv_card(title: str, rows: list[tuple[str, str]], width=165 * mm):
        """Structured key/value card with green header."""
        safe_rows = rows or [("—", "—")]
        t = Table(
            [[Paragraph(f"<b>{_esc(title)}</b>", styles["tbl_h"])]]
            + [
                [
                    Paragraph(_esc(k), styles["card_label"]),
                    Paragraph(
                        _esc(v if v not in (None, "") else "—"),
                        styles["card_value"],
                    ),
                ]
                for k, v in safe_rows
            ],
            colWidths=[38 * mm, width - 38 * mm],
        )
        t.setStyle(
            TableStyle(
                [
                    ("SPAN", (0, 0), (-1, 0)),
                    ("BACKGROUND", (0, 0), (-1, 0), HexColor("#1f4d38")),
                    ("BACKGROUND", (0, 1), (0, -1), HexColor("#eef6ee")),
                    ("ROWBACKGROUNDS", (1, 1), (1, -1), [HexColor("#f7fbf7"), white]),
                    ("BOX", (0, 0), (-1, -1), 0.6, HexColor("#cfe0cf")),
                    ("INNERGRID", (0, 0), (-1, -1), 0.3, HexColor("#e2eee2")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        return KeepTogether([t, Spacer(1, 2 * mm)])

    def make_table(data, col_widths, header_bg="#1f4d38"):
        t = Table(data, colWidths=col_widths, repeatRows=1)
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), HexColor(header_bg)),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("GRID", (0, 0), (-1, -1), 0.4, HexColor("#cfd8cf")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [HexColor("#f7fbf7"), white]),
                ]
            )
        )
        return t

    def p(text, style="body"):
        return Paragraph(text, styles[style])

    def hr():
        return HRFlowable(
            width="100%",
            thickness=0.7,
            color=HexColor("#cfe0cf"),
            spaceBefore=1,
            spaceAfter=5,
        )

    def bullets(items: list[Any], empty: str = AI_FAIL):
        elems = []
        cleaned = [str(x).strip() for x in (items or []) if str(x).strip()]
        if not cleaned:
            elems.append(p(f"<font color='#7a3a00'>{_esc(empty)}</font>", "small"))
            return elems
        for it in cleaned:
            elems.append(p(f"• {_esc(it)}", "bullet"))
        return elems

    def ai_block(title: str, body: str):
        text = (body or "").strip() or AI_FAIL
        if ai_fail and AI_FAIL not in text:
            text = f"{AI_FAIL}：{text}" if text else AI_FAIL
        box = Table(
            [
                [Paragraph(f"<b>{_esc(title)}</b>", styles["h2"])],
                [Paragraph(_esc(text), styles["body"])],
            ],
            colWidths=[165 * mm],
        )
        box.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), HexColor("#fffaf0")),
                    ("BOX", (0, 0), (-1, -1), 0.5, HexColor("#e8c48a")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        return KeepTogether([box, Spacer(1, 2 * mm)])

    def img(name, w=165 * mm, ratio=0.34, caption=None):
        path = chart_paths.get(name)
        elems = []
        if path and Path(path).exists():
            elems.append(Image(str(path), width=w, height=w * ratio))
            if caption:
                elems.append(Paragraph(_esc(caption), styles["small"]))
            elems.append(Spacer(1, 1.5 * mm))
        return elems

    def footer(c, doc):
        c.saveState()
        c.setStrokeColor(HexColor("#d7e3d7"))
        c.setLineWidth(0.6)
        c.line(16 * mm, 12 * mm, A4[0] - 16 * mm, 12 * mm)
        c.setFont("CN", 8)
        c.setFillColor(HexColor("#788878"))
        c.drawString(16 * mm, 7 * mm, f"选地体检 · 程序事实+AI解读 · {title_suffix}")
        c.drawRightString(A4[0] - 16 * mm, 7 * mm, f"{doc.page}")
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

    # ── 1. AI选地综合评价（封面）──
    story.append(Spacer(1, 3 * mm))
    story.append(p("一、AI选地综合评价", "cover_title"))
    story.append(p("程序计算评分 · AI 仅解读 · 需田间确认", "cover_sub"))
    story.append(Spacer(1, 2 * mm))
    story.append(hr())

    meta = [
        ["地块", f"{field.get('name')}（约 {area_mu} 亩 / {area_ha} 公顷）"],
        ["位置", field.get("location") or "—"],
        ["作物", crop_label],
        ["边界", field.get("boundary") or "—"],
        ["数据时段", risk.get("period") or "—"],
        ["报告时间", now_str],
    ]
    meta_data = [[cell(a, "tbl_b"), cell(b, "tbl_c")] for a, b in meta]
    mt = Table(meta_data, colWidths=[28 * mm, 140 * mm])
    mt.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), HexColor("#eef6ee")),
                ("ROWBACKGROUNDS", (1, 0), (1, -1), [HexColor("#f7fbf7"), white]),
                ("BOX", (0, 0), (-1, -1), 0.6, HexColor("#cfe0cf")),
                ("INNERGRID", (0, 0), (-1, -1), 0.3, HexColor("#e2eee2")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.append(mt)
    story.append(Spacer(1, 3 * mm))
    story.append(p("综合评分（程序计算，AI 不改分）", "center"))
    story.append(Spacer(1, 1.5 * mm))
    story.append(ScoreBadge(ov["score"], ov["light"], ov["grade"]))
    story.append(Spacer(1, 2 * mm))
    story.append(p(f"<b>程序一句话：</b>{_esc(ov.get('one_liner') or '')}", "body"))
    story.append(Spacer(1, 2 * mm))
    story.append(ai_block("AI 总体评价", _ai_text(overall_ai.get("evaluation"))))
    story.append(sub_title("1. 优势"))
    story += bullets(overall_ai.get("strengths") or [])
    story.append(sub_title("2. 主要风险"))
    story += bullets(overall_ai.get("main_risks") or [])
    story.append(sub_title("3. 核心建议（含依据）"))
    story += bullets(overall_ai.get("core_advice") or [])
    story.append(
        p(
            "分数怎么读：≥70 不错（绿），55–69 一般（黄），&lt;55 要盯紧（红）。"
            "AI 不重算分数。",
            "small",
        )
    )

    # ── 目录（薄页）──
    story.append(PageBreak())
    story.append(p("目录", "h1"))
    story.append(hr())
    story.append(
        p(
            "本报告为程序事实 + AI 解读；各章标题采用中文序数编号。页码见页脚。",
            "small",
        )
    )
    story.append(Spacer(1, 2 * mm))
    toc_rows = [
        [
            cell("章节", "tbl_h"),
            cell("内容提要", "tbl_h"),
        ]
    ]
    for title, blurb in TOC_ENTRIES:
        toc_rows.append([cell(title, "tbl_b"), cell(blurb, "tbl_c")])
    story.append(make_table(toc_rows, [55 * mm, 110 * mm]))
    story.append(Spacer(1, 3 * mm))
    story.append(
        p(
            "阅读建议：先看一、三了解总分与短板，再看四的异常证据表核对“发生了什么”。",
            "small",
        )
    )

    # ── 2. 地块基础画像 ──
    story.append(PageBreak())
    story.append(section_title("二、地块基础画像"))
    story.append(hr())
    season_months = sorted(
        (scorecard.get("method") or {}).get("season_months") or [6, 7, 8, 9]
    )
    peak_months = sorted((scorecard.get("method") or {}).get("peak_months") or [7, 8])
    season_txt = (
        f"默认{season_months[0]}–{season_months[-1]}月"
        f"；峰值{peak_months[0]}–{peak_months[-1]}月"
    )
    story.append(
        kv_card(
            "位置 / 面积 / 作物 / 生育季",
            [
                ("位置", field.get("location") or "—"),
                ("面积", f"约 {area_mu} 亩（{area_ha} 公顷）"),
                ("作物", crop_label),
                ("生育季", season_txt),
                ("数据时段", risk.get("period") or "—"),
            ],
        )
    )
    story.append(
        kv_card(
            "土壤关键指标（程序）",
            [
                (
                    "质地",
                    soil_texture_zh(soil.get("dominant_texture")) or "—",
                ),
                (
                    "pH",
                    (
                        str(soil.get("avg_ph"))
                        if soil.get("avg_ph") is not None
                        else "—"
                    ),
                ),
                (
                    "排水",
                    soil_drainage_zh(soil.get("drainage_class")) or "—",
                ),
                (
                    AWC_LABEL_ZH,
                    (
                        f"{soil.get('rootzone_awc_mm')} mm"
                        if soil.get("rootzone_awc_mm") is not None
                        else "—"
                    ),
                ),
                (
                    "渍水风险",
                    (
                        str(soil.get("waterlogging_risk"))
                        if soil.get("waterlogging_risk") is not None
                        else "—"
                    ),
                ),
            ],
        )
    )
    wh_plain = analysis.get("weather_history_plain") or ""
    climate_rows = [("气候本底摘要", wh_plain or "—")]
    if weather_summary:
        climate_rows.extend(
            [
                (
                    "近月热胁迫",
                    (
                        f"{weather_summary.get('heat_stress_days')} 天"
                        if weather_summary.get("heat_stress_days") is not None
                        else "—"
                    ),
                ),
                (
                    "近月水分盈亏",
                    (
                        f"{weather_summary.get('water_deficit_mm')} mm"
                        if weather_summary.get("water_deficit_mm") is not None
                        else "—"
                    ),
                ),
            ]
        )
    story.append(kv_card("气候基线（程序）", climate_rows))
    story.append(
        ai_block(
            "（一）区域农业特征",
            _ai_text(portrait_ai.get("regional_ag_traits")),
        )
    )
    story.append(ai_block("（二）作物适配", _ai_text(portrait_ai.get("crop_fit"))))
    story.append(sub_title("（三）限制因素"))
    story += bullets(portrait_ai.get("limits") or [])

    # ── 3. 综合评分解释 ──
    story.append(PageBreak())
    story.append(section_title("三、综合评分解释"))
    story.append(hr())
    story.append(
        p(
            "以下分数均由程序算法给出；AI 只解释高低维度与改进方向，并须引用数据。",
            "small",
        )
    )
    story.append(Spacer(1, 1.5 * mm))
    # Radar is primary; compact legend under chart (not a full dimension table)
    radar_elems = img(
        "score_radar.png", w=120 * mm, ratio=0.92, caption="六维综合评分雷达图（程序）"
    )
    if radar_elems:
        story += radar_elems
    else:
        story.append(p("（雷达图暂缺：六维分数不完整时跳过）", "small"))
    legend_rows = [
        [
            cell("维度", "tbl_h"),
            cell("分数", "tbl_h"),
            cell("灯", "tbl_h"),
        ]
    ]
    for key in DIM_ORDER:
        d = dims.get(key) or {}
        legend_rows.append(
            [
                cell(DIM_TITLE.get(key, d.get("name") or key), "tbl_b"),
                cell(str(d.get("score", "—")), "tbl_b"),
                cell(LIGHT_WORD.get(d.get("light"), d.get("light") or "—"), "tbl_b"),
            ]
        )
    story.append(make_table(legend_rows, [50 * mm, 30 * mm, 30 * mm]))
    story.append(Spacer(1, 1.5 * mm))
    story.append(
        p("虚线：绿阈 70 / 黄阈 55。表仅作图例，解读见下方 AI 小节。", "small")
    )
    story.append(Spacer(1, 1.5 * mm))
    story.append(sub_title("1. 偏高维度"))
    story += bullets(score_ai.get("high_dims") or [])
    story.append(sub_title("2. 偏低维度"))
    story += bullets(score_ai.get("low_dims") or [])
    story.append(sub_title("3. 最大驱动因素"))
    story += bullets(score_ai.get("biggest_drivers") or [])
    story.append(sub_title("4. 如何改进"))
    story += bullets(score_ai.get("how_to_improve") or [])

    # ── 4. 遥感长势 ──
    story.append(PageBreak())
    story.append(section_title("四、遥感长势"))
    story.append(hr())
    story.append(
        p(
            f"峰值期均 NDVI≈{rs.get('peak_ndvi_mean')}；淡季≈{rs.get('offseason_ndvi_mean')}；"
            f"生育期场景 {(rs.get('counts') or {}).get('season_scenes') or risk.get('n_season_scenes') or '—'} 景。"
            "评估按生育期口径，不用全年平均。",
            "body",
        )
    )
    story += img("ndvi.png", w=155 * mm, ratio=0.28, caption="NDVI 绿度曲线（程序）")
    story += img("evi.png", w=155 * mm, ratio=0.28, caption="EVI 交叉确认（程序）")
    shares = analysis.get("ndvi_grade_shares") or {}
    if shares.get("n"):
        pct = shares.get("pct") or {}
        story.append(
            p(
                f"生育期绿度等级：优 {pct.get('优', 0)}% / 良 {pct.get('良', 0)}% / "
                f"中 {pct.get('中', 0)}% / 差 {pct.get('差', 0)}%"
                f"（共 {shares.get('n')} 景）。",
                "body",
            )
        )
    if chart_paths.get("ndvi_grade_shares.png"):
        story += img("ndvi_grade_shares.png", w=95 * mm, ratio=0.62, caption="等级占比")
    pheno = analysis.get("phenology_stage_summary") or []
    if pheno:
        prow = [
            [
                cell("阶段", "tbl_h"),
                cell("代表日", "tbl_h"),
                cell("均NDVI", "tbl_h"),
                cell("相对上阶段", "tbl_h"),
                cell("等级", "tbl_h"),
            ]
        ]
        for stg in pheno:
            mean = stg.get("mean_ndvi")
            grade = "疑似裸地" if stg.get("likely_bare") else (stg.get("grade") or "—")
            prow.append(
                [
                    cell(stg.get("label") or stg.get("key") or "—", "tbl_c"),
                    cell(str(stg.get("date") or "—"), "tbl_c"),
                    cell(f"{mean:.3f}" if mean is not None else "—", "tbl_c"),
                    cell(stg.get("trend_vs_prev") or "—", "tbl_c"),
                    cell(grade, "tbl_c"),
                ]
            )
        story.append(make_table(prow, [30 * mm, 30 * mm, 26 * mm, 30 * mm, 44 * mm]))
    if chart_paths.get("ndvi_stage_trend.png"):
        story += img(
            "ndvi_stage_trend.png",
            w=145 * mm,
            ratio=0.40,
            caption="生育阶段绿度走势（程序）",
        )
    em = analysis.get("emergence") or {}
    if em.get("note_zh"):
        story.append(p(f"<b>{_esc(em.get('note_zh'))}</b>", "body"))
    elif analysis.get("emergence_note"):
        story.append(p(f"<b>{_esc(analysis.get('emergence_note'))}</b>", "body"))
    story.append(
        ai_block(
            "物候与长势解读",
            _ai_text(rs_ai.get("phenology_normality")),
        )
    )

    # 异常点：AI 农户可读卡片为主，程序证据表作脚注
    story.append(sub_title("1. 异常点（AI 解读）"))
    events = analysis.get("risk_events_evidence") or risk.get("events") or []
    ai_anoms = rs_ai.get("anomalies") or []
    # Build lookup from program events for period/stage footnotes on cards
    ev_by_id = {
        str(ev.get("id")): ev for ev in events if isinstance(ev, dict) and ev.get("id")
    }
    if ai_anoms:
        for card in ai_anoms:
            if isinstance(card, dict) and card.get("problem"):
                eid = str(card.get("event_id") or "")
                ev = ev_by_id.get(eid) or {}
                period = ev.get("period_full") or ""
                stage = ev.get("stage") or ""
                head = eid or "异常"
                if period:
                    head += f" · {period}"
                if stage:
                    head += f" · {stage}"
                rows = [
                    ("问题是什么", card.get("problem") or "—"),
                    ("更可能原因", card.get("likely_cause") or "—"),
                    ("判断依据", card.get("basis") or "—"),
                    ("把握", card.get("confidence") or "—"),
                ]
                story.append(kv_card(head, rows))
            else:
                story.append(p(f"• {_esc(card)}", "bullet"))
    elif events:
        story.append(
            p(
                "已有程序异常证据，但 AI 事件卡片未返回；请见下方程序证据表。",
                "small",
            )
        )
    else:
        story.append(p("程序未检出生育期长势/偏湿聚类事件。", "small"))

    story.append(sub_title("1b. 程序证据表（技术脚注）"))
    if events:
        erows = [
            [
                cell("事件", "tbl_h"),
                cell("时段", "tbl_h"),
                cell("表现", "tbl_h"),
                cell("同期天气/水分", "tbl_h"),
                cell("生育阶段", "tbl_h"),
            ]
        ]
        for ev in events[:8]:
            if not isinstance(ev, dict):
                continue
            eid = ev.get("id") or "—"
            period = ev.get("period_full") or (
                f"{ev.get('start', '')}～{ev.get('end', '')}"
            )
            perf = ev.get("performance") or ev.get("type") or "—"
            wx = ev.get("weather_moisture") or "—"
            stage = ev.get("stage") or "—"
            erows.append(
                [
                    cell(str(eid), "tbl_b"),
                    cell(str(period), "tbl_c"),
                    cell(str(perf), "tbl_c"),
                    cell(str(wx), "tbl_c"),
                    cell(str(stage), "tbl_c"),
                ]
            )
        story.append(make_table(erows, [14 * mm, 32 * mm, 52 * mm, 42 * mm, 25 * mm]))
        story.append(
            p(
                "说明：表现中的 NDVI/EVI/NDWI 取自事件窗内已观测场景均值；"
                "无月尺度天气时仅给水分指数或地块级摘要，不编造日降水。"
                "上表为程序事实，供核对；农户请优先阅读上方 AI 卡片。",
                "small",
            )
        )
    else:
        story.append(p("无程序异常事件行。", "small"))

    story.append(sub_title("2. 可能原因（排序）"))
    ranked = rs_ai.get("ranked_causes") or []
    if ranked:
        for item in ranked:
            if isinstance(item, dict):
                line = f"{item.get('rank', '')}. {_esc(item.get('cause') or '')}"
                if item.get("evidence"):
                    line += f"（依据：{_esc(item.get('evidence'))}）"
                story.append(p(f"• {line}", "bullet"))
            else:
                story.append(p(f"• {_esc(item)}", "bullet"))
    else:
        story.append(p(f"<font color='#7a3a00'>{AI_FAIL}</font>", "small"))

    # ── 5. 空间异常 ──
    story.append(PageBreak())
    story.append(section_title("五、空间异常"))
    story.append(hr())
    has_panel = bool(chart_paths.get("ndvi_stages_panel.png"))
    phenology_name = None
    for key in chart_paths:
        if str(key).startswith("ndvi_phenology_") and str(key).endswith(".png"):
            phenology_name = key
            break
    if phenology_name is None and chart_paths.get("ndvi_phenology.png"):
        phenology_name = "ndvi_phenology.png"
    if phenology_name:
        story += img(phenology_name, w=165 * mm, ratio=0.32, caption="生育期绿度曲线")
    if has_panel:
        story += img(
            "ndvi_stages_panel.png",
            w=150 * mm,
            ratio=0.70,
            caption="四阶段空间对比（有影像时）",
        )
    else:
        story.append(
            p(
                "说明：暂无可用的像元/真彩预览时，仅保留生育阶段曲线；"
                "空间对比图待影像回填后自动补上。",
                "small",
            )
        )
    story.append(sub_title("1. 需关注区域"))
    zones = spatial_ai.get("watch_zones") or []
    why = spatial_ai.get("why") or []
    if zones:
        story += bullets(zones)
    elif spatial_ai.get("no_hotspot") or why:
        # Empty watch_zones with why/no_hotspot is a valid AI answer, not a fail
        story.append(
            p(
                "未发现需特别关注的空间异质斑块（程序未见局部异常；"
                "异常事件多为全地块同步，见下方原因）。",
                "body",
            )
        )
    else:
        story += bullets([], empty=AI_FAIL)
    story.append(sub_title("2. 原因"))
    story += bullets(why)
    story.append(
        ai_block(
            "时间连续性提示",
            _ai_text(
                spatial_ai.get("temporal_caveat"),
                "单景不足以定论，需结合多时相连续观测与田间核实。",
            ),
        )
    )

    # ── 6. 土壤 ──
    story.append(PageBreak())
    story.append(section_title("六、土壤"))
    story.append(hr())
    soil_plain = translate_soil_jargon(analysis.get("soil_analysis_plain") or "")
    if soil_plain:
        story.append(p(f"<b>程序土壤分析：</b>{_esc(soil_plain)}", "body"))
    else:
        tex = soil_texture_zh(soil.get("dominant_texture")) or "—"
        drain = soil_drainage_zh(soil.get("drainage_class")) or "—"
        story.append(
            p(
                f"质地 {_esc(tex)}；"
                f"pH {soil.get('avg_ph') if soil.get('avg_ph') is not None else '—'}；"
                f"排水 {_esc(drain)}；"
                f"{_esc(AWC_LABEL_ZH)} "
                f"{soil.get('rootzone_awc_mm') if soil.get('rootzone_awc_mm') is not None else '—'}；"
                f"渍水风险 {soil.get('waterlogging_risk') if soil.get('waterlogging_risk') is not None else '—'}。",
                "body",
            )
        )
    story.append(Spacer(1, 2 * mm))
    soil_rows = soil_ai.get("indicators_to_farm") or []
    srows = [
        [
            cell("指标", "tbl_h"),
            cell("田间影响", "tbl_h"),
            cell("管理方向", "tbl_h"),
        ]
    ]
    if soil_rows:
        for r in soil_rows:
            if isinstance(r, dict):
                srows.append(
                    [
                        cell(
                            translate_soil_jargon(r.get("indicator") or "—"),
                            "tbl_b",
                        ),
                        cell(
                            translate_soil_jargon(r.get("farm_impact") or "—"),
                            "tbl_c",
                        ),
                        cell(
                            translate_soil_jargon(r.get("management") or "—"),
                            "tbl_c",
                        ),
                    ]
                )
            else:
                srows.append(
                    [cell(str(r), "tbl_c"), cell("—", "tbl_c"), cell("—", "tbl_c")]
                )
    else:
        srows.append(
            [
                cell(AI_FAIL, "tbl_c"),
                cell(AI_FAIL, "tbl_c"),
                cell(AI_FAIL, "tbl_c"),
            ]
        )
    story.append(make_table(srows, [40 * mm, 62 * mm, 63 * mm]))
    story.append(p("说明：无农艺模型时不给具体施肥量（kg/亩）。", "small"))

    # ── 7. 气候风险 ──
    story.append(PageBreak())
    story.append(section_title("七、气候风险"))
    story.append(hr())
    if wh_plain:
        story.append(p(f"<b>历史气候（程序）：</b>{_esc(wh_plain)}", "body"))
    story.append(
        p(
            f"近月热胁迫天 {weather_summary.get('heat_stress_days', '—')}；"
            f"水分盈亏 {weather_summary.get('water_deficit_mm', '—')} mm；"
            f"遥感涝信号 {_esc(rs.get('rs_flood_level') or '—')}；"
            f"遥感旱信号 {_esc(rs.get('rs_drought_level') or '—')}。",
            "body",
        )
    )
    if chart_paths.get("weather_history.png"):
        story += img(
            "weather_history.png",
            w=145 * mm,
            ratio=0.36,
            caption="历史降水/温度（程序）",
        )
    # Flood evidence (hard)
    if (
        flood_evidence
        and int(flood_evidence.get("absolute_open_water_scenes") or 0) > 0
    ):
        story.append(sub_title("1. 明水面涝证据（硬证据）"))
        n_all = flood_evidence.get("absolute_open_water_scenes")
        story.append(
            p(
                f"生育期内卫星见明水面 <b>{n_all}</b> 景。"
                f"{_esc(flood_evidence.get('analysis') or '')}",
                "body",
            )
        )
        if chart_paths.get("flood_rgb_collage.png"):
            story += img(
                "flood_rgb_collage.png",
                w=140 * mm,
                ratio=0.40,
                caption="明水面场景真彩",
            )
    story.append(sub_title("2. 风险存在（非已发生灾害）"))
    story += bullets(climate_ai.get("risk_present") or [])
    story.append(sub_title("3. 灾害已发生（须有硬证据）"))
    disasters = climate_ai.get("disaster_occurred") or []
    if disasters:
        story += bullets(disasters)
    else:
        story.append(p("无硬证据支撑的已发生灾害结论。", "small"))
    if climate_ai.get("notes"):
        story.append(ai_block("气候补充说明", _ai_text(climate_ai.get("notes"))))

    # ── 8. 种植管理建议 ──
    story.append(PageBreak())
    story.append(section_title("八、种植管理建议"))
    story.append(hr())
    story.append(
        ai_block(
            "品种方向",
            _ai_text(mgmt_ai.get("variety_direction")),
        )
    )
    story.append(sub_title("1. 播种与田间重点"))
    story += bullets(mgmt_ai.get("planting_focus") or [])
    story.append(sub_title("2. 水肥关注"))
    story += bullets(mgmt_ai.get("water_fertility_watch") or [])
    story.append(sub_title("3. 巡田建议"))
    story += bullets(mgmt_ai.get("scouting") or [])
    story.append(
        p("说明：不发明精确播期/施肥量/灌溉量；请结合当地农技与田间实测。", "small")
    )

    # ── 9. 产量潜力 ──
    story.append(PageBreak())
    story.append(section_title("九、产量潜力"))
    story.append(hr())
    level = yield_ai.get("level")
    if level in ("高", "中", "低"):
        story.append(p(f"<b>潜力等级（相对）：</b>{level}", "body"))
    else:
        story.append(
            p(
                "<b>潜力等级：</b>数据不足（无实测产量模型时仅可给 高/中/低，"
                "本报告暂不给出）。",
                "body",
            )
        )
    story.append(ai_block("依据说明", _ai_text(yield_ai.get("rationale"))))
    story.append(p("严禁虚构亩产数字。本页不含任何编造的产量数值。", "small"))

    # ── 10. 经营分析（与产量潜力同页，控制总页数 ≤10）──
    story.append(Spacer(1, 3 * mm))
    story.append(section_title("十、经营分析"))
    story.append(hr())
    available = bool(biz_ai.get("available"))
    if not available:
        story.append(
            p(
                "<b>数据不足：</b>当前无价格/成本/产量经营模型，"
                "不提供金额测算，避免误导。",
                "body",
            )
        )
        story.append(ai_block("说明", _ai_text(biz_ai.get("note"), "数据不足")))
    else:
        story.append(ai_block("经营解读", _ai_text(biz_ai.get("note"))))
    gaps = ai.get("evidence_gaps") or []
    if gaps:
        story.append(sub_title("1. 证据缺口"))
        story += bullets(gaps, empty="—")
    story.append(Spacer(1, 3 * mm))
    story.append(sub_title("2. 数据来源与置信"))
    story.append(
        p(
            "卫星：Sentinel-2 指数（OpenFarm / agri）。天气：Open-Meteo。"
            "土壤：SoilGrids。评分与图表由程序计算；AI 仅做农学解读。"
            f" 置信：{_esc((scorecard.get('confidence') or {}).get('plain') or '—')}",
            "small",
        )
    )
    if ai.get("error"):
        story.append(
            p(
                f"AI 状态：{_esc(ai.get('error'))}（事实页仍完整输出）。",
                "small",
            )
        )

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=12 * mm,
        bottomMargin=18 * mm,
        title=f"{field.get('name') or '地块'}选地体检",
        author="OpenFarm",
    )
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return out_path
