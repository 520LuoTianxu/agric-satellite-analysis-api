# -*- coding: utf-8 -*-
"""Reportlab PDF renderer for 选地体检（白话版）."""

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

LIGHT_WORD = {"绿": "不错", "黄": "一般", "红": "要盯紧"}
LIGHT_COLOR = {"绿": "#1b7a3d", "黄": "#c48a00", "红": "#c0392b"}
LIGHT_BG = {"绿": "#e8f6ee", "黄": "#fff7e0", "红": "#fdecea"}

CST = timezone(timedelta(hours=8))


def _register_fonts() -> None:
    if "CN" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("CN", str(FONT_PATH)))
        pdfmetrics.registerFont(TTFont("CNB", str(FONT_PATH)))


def _styles(ov_light: str) -> dict[str, ParagraphStyle]:
    return {
        "cover_title": ParagraphStyle(
            "cover_title",
            fontName="CNB",
            fontSize=22,
            leading=30,
            alignment=TA_CENTER,
            textColor=HexColor("#143d2b"),
        ),
        "cover_sub": ParagraphStyle(
            "cover_sub",
            fontName="CN",
            fontSize=11,
            leading=16,
            alignment=TA_CENTER,
            textColor=HexColor("#5a6a60"),
        ),
        "h1": ParagraphStyle(
            "h1",
            fontName="CNB",
            fontSize=14,
            leading=22,
            textColor=HexColor("#143d2b"),
            spaceBefore=8,
            spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "h2",
            fontName="CNB",
            fontSize=11.5,
            leading=17,
            textColor=HexColor("#1f4d38"),
            spaceBefore=6,
            spaceAfter=3,
        ),
        "body": ParagraphStyle(
            "body",
            fontName="CN",
            fontSize=10,
            leading=16,
            alignment=TA_JUSTIFY,
            textColor=HexColor("#222"),
        ),
        "small": ParagraphStyle(
            "small", fontName="CN", fontSize=8.5, leading=13, textColor=HexColor("#666")
        ),
        "center": ParagraphStyle(
            "center",
            fontName="CN",
            fontSize=11,
            leading=16,
            alignment=TA_CENTER,
            textColor=HexColor("#333"),
        ),
        "badge": ParagraphStyle(
            "badge",
            fontName="CNB",
            fontSize=12,
            leading=16,
            alignment=TA_CENTER,
            textColor=white,
        ),
        "card_body": ParagraphStyle(
            "card_body", fontName="CN", fontSize=9, leading=13.5, textColor=HexColor("#333")
        ),
        "tip": ParagraphStyle(
            "tip", fontName="CN", fontSize=9, leading=13, textColor=HexColor("#1f4d38")
        ),
        "tbl_h": ParagraphStyle(
            "tbl_h", fontName="CNB", fontSize=9, leading=12, textColor=white
        ),
        "tbl_c": ParagraphStyle(
            "tbl_c", fontName="CN", fontSize=8.5, leading=12, textColor=HexColor("#222")
        ),
        "tbl_b": ParagraphStyle(
            "tbl_b", fontName="CNB", fontSize=8.5, leading=12, textColor=HexColor("#222")
        ),
    }


class ScoreBadge(Flowable):
    def __init__(self, score, light, grade, width=170 * mm, height=38 * mm):
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
            y = self.height - 10 * mm - i * 9 * mm
            c.setFillColor(HexColor(col) if on else HexColor("#cfd8cf"))
            c.circle(12 * mm, y, 3.5 * mm if on else 2.8 * mm, fill=1, stroke=0)
        c.setFillColor(fg)
        c.setFont("CNB", 36)
        c.drawCentredString(self.width / 2 + 4 * mm, self.height / 2 + 2 * mm, f"{self.score}")
        c.setFont("CN", 11)
        c.setFillColor(HexColor("#444"))
        c.drawCentredString(
            self.width / 2 + 4 * mm,
            8 * mm,
            f"{self.grade} · {LIGHT_WORD.get(self.light, self.light)}",
        )


def _build_plain_dims(scorecard: dict, soil: dict, rs: dict, weather_summary: dict) -> dict:
    dims = {d["key"]: d for d in scorecard["dimensions"]}
    ph = soil.get("avg_ph")
    texture = soil.get("dominant_texture") or "土壤"
    drain = soil.get("drainage_class") or ""
    peak = rs.get("peak_ndvi_mean")
    off = rs.get("offseason_ndvi_mean")
    heat = weather_summary.get("heat_stress_days")
    wd = weather_summary.get("water_deficit_mm")

    crop_d = dims["crop"]
    soil_d = dims["soil"]
    vigor_d = dims["vigor"]
    weather_d = dims["weather"]
    wet_d = dims["wet_safety"]
    dry_d = dims["drought_safety"]

    soil_say = f"{texture}，"
    if "clay" in str(texture).lower() or "黏" in str(texture):
        soil_say += "能存水；"
    if "well" in drain.lower():
        soil_say += "排水还行。"
    else:
        soil_say += "排水一般。"
    if ph is not None:
        soil_say += f"酸碱度偏碱（大约 {float(ph):.1f}），" if float(ph) > 7.5 else f"酸碱度大约 {float(ph):.1f}，"
    if float(soil.get("waterlogging_risk") or 0) > 0.3:
        soil_say += "有一点点积水风险。"
    else:
        soil_say += "渍水风险不大。"

    vigor_say = (
        "只看夏天玉米旺长的时候（大概 6–9 月，尤其 7–8 月）。"
        f"那段时间地里「绿得程度」"
        + (
            f"达到较好水平（约 {peak}）"
            if peak and peak >= 0.65
            else (f"大约 {peak}" if peak is not None else "按生育期统计")
        )
        + "，"
    )
    if peak is not None and off is not None and peak > off + 0.25:
        vigor_say += "而且夏天明显比冬天绿——说明季节能对上，不是全年瞎平均。"
    else:
        vigor_say += "按生育期口径评估，不是全年瞎平均。"

    weather_say = weather_d["plain"]
    if heat:
        weather_say = f"近一个月温度正常偏热，有几天高温；" + (
            "雨水和蒸发差不多，墒情还过得去。" if abs(float(wd or 0)) < 20 else weather_d["plain"]
        )

    return {
        "crop": {
            "title": "适不适合种玉米",
            "say": (
                "这块地种玉米总体合适。"
                + (f"土偏碱一点、" if ph and float(ph) > 7.5 else "")
                + "但不至于种不了。"
            ),
            "tip": "选耐碱品种更稳妥；别指望当「完美高产地」。"
            if ph and float(ph) > 7.5
            else "按当地品种与水肥管理来即可。",
        },
        "soil": {
            "title": "土怎么样",
            "say": soil_say,
            "tip": "下雨后低洼处要看看会不会存水；干旱年要注意保墒。",
        },
        "vigor": {
            "title": "玉米季长得好不好",
            "say": vigor_say,
            "tip": "如果某一年夏天整片特别不绿，更可能是当年没种或绝产，不一定是「种得很差」。",
        },
        "weather": {
            "title": "天气压不压苗",
            "say": weather_say,
            "tip": "高温天注意玉米抽雄灌浆别缺水。",
        },
        "wet_safety": {
            "title": "怕不怕涝",
            "say": wet_d["plain"],
            "tip": "雨季仍可去低洼处看看排水，但这是日常田间管理，不是判定这块地涝灾频发。",
        },
        "drought_safety": {
            "title": "怕不怕旱",
            "say": dry_d["plain"],
            "tip": "旺季别大意断水仍是农事常识，但不等于这块地历史上旱过、更不是红灯定罪。",
        },
    }


def _watch_rows(soil: dict, rs: dict, scorecard: dict) -> list[list[str]]:
    rows = [["优先级", "问题", "人话理解"]]
    ph = soil.get("avg_ph")
    if ph is not None and float(ph) > 7.5:
        rows.append(
            [
                "中",
                "土偏碱",
                f"酸碱度大约 {float(ph):.1f}，玉米能种；选耐碱一点的品种更踏实。",
            ]
        )
    wet = next(d for d in scorecard["dimensions"] if d["key"] == "wet_safety")
    dry = next(d for d in scorecard["dimensions"] if d["key"] == "drought_safety")
    if wet["light"] == "绿" and dry["light"] == "绿":
        rows.append(
            [
                "低",
                "涝/旱提醒",
                "没有真涝真旱硬证据，两项已是绿灯。雨季看排水、旺季别断水仍是常识，不是历史定罪。",
            ]
        )
    else:
        rows.append(
            [
                "中",
                "涝/旱留意",
                f"怕涝 {wet['score']} / 怕旱 {dry['score']}。相对信号只作提醒，请结合田间核实。",
            ]
        )
    uncropped = rs.get("possible_uncropped_years") or []
    if uncropped:
        rows.append(
            [
                "中",
                "局部差异",
                f"部分年份峰值极低（{uncropped}），更像未种植/绝产，可对照阶段长势再核。",
            ]
        )
    else:
        rows.append(
            [
                "低",
                "局部差异",
                "若某年有抛荒，可对照生育期绿度曲线再核。",
            ]
        )
    return rows


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
) -> Path:
    _register_fonts()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    chart_paths = chart_paths or {}

    ov = scorecard["overall"]
    dims = {d["key"]: d for d in scorecard["dimensions"]}
    styles = _styles(ov["light"])
    plain = _build_plain_dims(scorecard, soil, rs, weather_summary)

    area_ha = float(field.get("area_ha") or 0)
    area_mu = round(area_ha * 15, 1)
    now_str = datetime.now(CST).strftime("%Y年%m月%d日 %H:%M")

    one_liner = ov.get("one_liner") or ""
    thinking_plain = (
        f"这块地大约 {area_mu} 亩，位于 {field.get('location') or '—'}，种的是夏玉米。"
        f"我们不看全年平均绿度（全年平均会被冬天拉低，不公平），"
        f"只看玉米真正生长的夏天：7–8 月平均绿度大约 {rs.get('peak_ndvi_mean')}；"
        f"冬天大约 {rs.get('offseason_ndvi_mean')}。"
        f"{ov.get('thinking') or ''}"
    )

    def cell(text, style="tbl_c"):
        return Paragraph(str(text), styles[style])

    def make_table(data, col_widths, header_bg="#1f4d38"):
        t = Table(data, colWidths=col_widths, repeatRows=1)
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), HexColor(header_bg)),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
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
            width="100%", thickness=0.8, color=HexColor("#cfe0cf"), spaceBefore=2, spaceAfter=8
        )

    def dim_card(key):
        d = dims[key]
        pl = plain[key]
        light = d["light"]
        bg = LIGHT_BG[light]
        fg = LIGHT_COLOR[light]
        header = Table(
            [
                [
                    Paragraph(
                        f"<font color='white'><b>{pl['title']}</b></font>", styles["badge"]
                    ),
                    Paragraph(
                        f"<font color='white'><b>{d['score']} 分 · {LIGHT_WORD[light]}</b></font>",
                        styles["badge"],
                    ),
                ]
            ],
            colWidths=[110 * mm, 55 * mm],
        )
        header.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), HexColor(fg)),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ]
            )
        )
        body = Table(
            [
                [Paragraph(pl["say"], styles["card_body"])],
                [Paragraph(f"<b>建议：</b>{pl['tip']}", styles["tip"])],
            ],
            colWidths=[165 * mm],
        )
        body.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), HexColor(bg)),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("BOX", (0, 0), (-1, -1), 0.4, HexColor(fg)),
                ]
            )
        )
        wrap = Table([[header], [body]], colWidths=[165 * mm])
        wrap.setStyle(
            TableStyle(
                [
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        return KeepTogether([wrap, Spacer(1, 3.5 * mm)])

    def img(name, w=165 * mm, ratio=0.36, caption=None):
        path = chart_paths.get(name)
        elems = []
        if path and Path(path).exists():
            elems.append(Image(str(path), width=w, height=w * ratio))
            if caption:
                elems.append(Paragraph(caption, styles["small"]))
            elems.append(Spacer(1, 2 * mm))
        return elems

    def footer(c, doc):
        c.saveState()
        c.setStrokeColor(HexColor("#d7e3d7"))
        c.setLineWidth(0.6)
        c.line(16 * mm, 12 * mm, A4[0] - 16 * mm, 12 * mm)
        c.setFont("CN", 8)
        c.setFillColor(HexColor("#788878"))
        c.drawString(16 * mm, 7 * mm, f"选地体检（白话版）· {title_suffix}")
        c.drawRightString(A4[0] - 16 * mm, 7 * mm, f"{doc.page}")
        c.restoreState()

    story = []
    story.append(Spacer(1, 8 * mm))
    story.append(p("地块选地体检报告", "cover_title"))
    story.append(p("白话版 · 给非遥感专业的人看的", "cover_sub"))
    story.append(Spacer(1, 3 * mm))
    story.append(hr())

    meta = [
        ["地块", f"{field.get('name')}（约 {area_mu} 亩 / {area_ha} 公顷）"],
        ["位置", field.get("location") or "—"],
        ["作物", field.get("crop_label") or "夏玉米（按 6–9 月生育期、7–8 月旺长期来看）"],
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
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(mt)
    story.append(Spacer(1, 6 * mm))
    story.append(p("总成绩（越高越好）", "center"))
    story.append(Spacer(1, 2 * mm))
    story.append(ScoreBadge(ov["score"], ov["light"], ov["grade"]))
    story.append(Spacer(1, 4 * mm))
    story.append(p(f"<b>一句话：</b>{one_liner}", "body"))
    story.append(Spacer(1, 2 * mm))
    story.append(p(f"<b>怎么理解：</b>{thinking_plain}", "body"))
    story.append(Spacer(1, 3 * mm))
    story.append(
        p(
            "分数怎么读：70 分以上算不错（绿灯），55–69 一般（黄灯），低于 55 要盯紧（红灯）。",
            "small",
        )
    )
    story.append(
        p(
            "这份报告回答三件事：①能不能种玉米 ②夏天长势行不行 ③涝和旱要不要小心。",
            "small",
        )
    )

    story.append(PageBreak())
    story.append(p("分项体检（点开就能懂）", "h1"))
    story.append(hr())
    story.append(p("下面六项，每项都有分数、人话说明，和一句实地建议。", "body"))
    story.append(Spacer(1, 3 * mm))
    for key in ["crop", "soil", "vigor", "weather", "wet_safety", "drought_safety"]:
        story.append(dim_card(key))

    rows = [
        [cell("看什么", "tbl_h"), cell("分数", "tbl_h"), cell("灯", "tbl_h"), cell("白话", "tbl_h")]
    ]
    for key in ["crop", "soil", "vigor", "weather", "wet_safety", "drought_safety"]:
        d = dims[key]
        rows.append(
            [
                cell(plain[key]["title"], "tbl_b"),
                cell(str(d["score"]), "tbl_b"),
                cell(LIGHT_WORD[d["light"]], "tbl_b"),
                cell(plain[key]["say"], "tbl_c"),
            ]
        )
    st = make_table(rows, [32 * mm, 16 * mm, 16 * mm, 101 * mm])
    for i, key in enumerate(
        ["crop", "soil", "vigor", "weather", "wet_safety", "drought_safety"], start=1
    ):
        st.setStyle(
            TableStyle(
                [("TEXTCOLOR", (2, i), (2, i), HexColor(LIGHT_COLOR[dims[key]["light"]]))]
            )
        )
    story.append(KeepTogether([p("分数一览", "h2"), st]))

    story.append(PageBreak())
    story.append(p("最该盯的几件事", "h1"))
    story.append(hr())
    story.append(
        p(
            "综合来看，请优先看土壤限制因子与生育期管理；"
            "涝旱没有成灾硬证据时，不按红灯定罪。",
            "body",
        )
    )
    story.append(Spacer(1, 3 * mm))

    watch_rows = _watch_rows(soil, rs, scorecard)
    watch_data = []
    for i, r in enumerate(watch_rows):
        if i == 0:
            watch_data.append([cell(x, "tbl_h") for x in r])
        else:
            watch_data.append([cell(r[0], "tbl_b"), cell(r[1], "tbl_b"), cell(r[2], "tbl_c")])
    wt = make_table(watch_data, [18 * mm, 28 * mm, 119 * mm])
    story.append(KeepTogether([p("优先看这些", "h2"), wt]))
    story.append(Spacer(1, 5 * mm))

    def _is_false_alarm(e):
        start = e.get("start") or ""
        mon = int(start[5:7]) if len(start) >= 7 else 0
        day = int(start[8:10]) if len(start) >= 10 else 0
        typ = e.get("type") or ""
        if "长势" in typ and mon == 6 and day <= 15:
            return True
        if "长势" in typ and mon == 9 and day >= 20:
            return True
        return False

    evs_all = risk.get("events") or []
    evs = [e for e in evs_all if not _is_false_alarm(e)][:5]
    story.append(p("玉米季里值得记一笔的变化", "h2"))
    story.append(p("已去掉苗期偏低、成熟回落这类正常现象；只保留更值得留意的时段。", "small"))
    story.append(Spacer(1, 2 * mm))
    if evs:
        erows = [
            [cell("大概时间", "tbl_h"), cell("发生了什么", "tbl_h"), cell("下田可以看", "tbl_h")]
        ]
        for e in evs:
            typ = e.get("type") or ""
            if "长势" in typ:
                what = "夏天绿得偏少（不是苗期/成熟那种正常偏低）"
                look = "看苗情、密度、是否缺肥"
            elif "偏湿" in typ:
                what = "这段时间显得比平常湿一点"
                look = "看低洼处有无积水、沟通不通"
            else:
                what = typ
                look = "对照田间实际情况"
            erows.append(
                [
                    cell(f"{e.get('start')} 至 {e.get('end')}", "tbl_c"),
                    cell(what, "tbl_c"),
                    cell(look, "tbl_c"),
                ]
            )
        story.append(make_table(erows, [42 * mm, 68 * mm, 55 * mm]))
    else:
        story.append(p("生育期里没有需要特别点名的异常段。", "body"))
    story.append(Spacer(1, 3 * mm))
    story.append(
        p("说明：单日弱信号不必紧张；连续多天且正好赶在旺长期，才更值得下田看一眼。", "small")
    )

    story.append(PageBreak())
    story.append(p("图：绿度怎么随季节变化", "h1"))
    story.append(hr())
    story.append(
        p(
            "下面曲线里，浅绿色阴影是玉米生育期（6–9 月），深一点的是旺长期（7–8 月）。"
            "虚线是「生育期里的偏低线」，不是全年平均线。看图时请盯夏天，别被冬天的低值吓到。",
            "body",
        )
    )
    story += img("ndvi.png", caption="绿度（NDVI）：越高通常苗越旺；请看阴影里的夏天")
    story += img("evi.png", caption="另一路绿度（EVI）：用来交叉确认夏天是否真的旺")
    story += img("ndwi.png", caption="干湿相关（NDWI/MNDWI）：玉米季里突然偏高且苗又弱，要怀疑积水")
    story += img("monthly.png", caption="哪些月份更容易出现需要留意的信号（灰色月份不在玉米季）")

    story.append(PageBreak())
    story.append(p("看不懂？先看这里", "h1"))
    story.append(hr())
    faqs = [
        (
            "为什么不看全年平均绿度？",
            "玉米冬天本来就不绿。把冬天算进去，好田也会被拉很低。所以我们只看它该绿的夏天。",
        ),
        (
            "成熟时绿度下降正常吗？",
            "正常。灌浆成熟后叶片变黄，NDVI 会从高峰掉下来。关键是旺长期够不够高、下降是不是来得过早或掉得过狠。",
        ),
        (
            "NDVI 是什么？",
            "可以粗浅理解成「卫星看到的绿得程度」。数字高，通常植被更旺。本报告尽量用「绿度」来说人话。",
        ),
        (
            "为什么有的地方夏天特别不绿？",
            "可能是缺水缺肥、渍害，也可能是那一块当年根本没种。我们会尽量把「可能没种」单独标出来。",
        ),
        (
            "红灯是不是不能种？",
            "不是。红灯是「这项要小心」，不是死刑判决。没有硬涝/旱证据时，这两项不会轻易打红灯。",
        ),
        (
            "能当买地依据吗？",
            "不能单独当判决书。土壤/卫星/天气都是公开数据估算，买地前还要田间实测和权属核查。",
        ),
    ]
    for q, a in faqs:
        story.append(p(f"<b>问：{q}</b>", "h2"))
        story.append(p(f"答：{a}", "body"))

    story.append(Spacer(1, 4 * mm))
    story.append(p("数据从哪来", "h2"))
    story.append(
        p(
            "卫星：Sentinel-2 绿度等指数（OpenFarm / agri 回填）。天气：Open-Meteo。土壤：SoilGrids。"
            "长势口径：夏玉米 6–9 月，旺长 7–8 月。生成工具：OpenFarm 选地体检。",
            "small",
        )
    )
    story.append(p(f"置信提示：{scorecard.get('confidence', {}).get('plain', '')}", "small"))

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=12 * mm,
        bottomMargin=18 * mm,
        title=f"{field.get('name') or '地块'}选地体检（白话版）",
        author="OpenFarm",
    )
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return out_path
