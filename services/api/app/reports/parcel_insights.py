"""从冻结快照绘制中文营销简报，所有数值与页面历史结果一致。"""

from datetime import date
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

GREEN = colors.HexColor("#17745b")
INK = colors.HexColor("#18372e")
MUTED = colors.HexColor("#60756c")
PROGRESS = {
    "unknown": "证据不足",
    "harvest_signal": "已呈收获表现",
    "growth_signal": "有生长表现",
    "needs_check": "待现场核查",
}


def font_name():
    from app.reports.land_assessment.paths import FONT_PATH

    if "InsightsCN" not in pdfmetrics.getRegisteredFontNames():
        if FONT_PATH.exists():
            pdfmetrics.registerFont(TTFont("InsightsCN", str(FONT_PATH)))
        else:
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
            return "STSong-Light"
    return "InsightsCN"


def shown(value, digits=3):
    return (
        "暂无"
        if value is None
        else f"{value:.{digits}f}"
        if isinstance(value, (int, float))
        else str(value)
    )


def curve(item, font):
    """相同 NDVI 纵轴展示历史曲线；超过 35 天的缺景不连线。"""
    drawing = Drawing(490, 130)
    points = item["series"]
    if not points:
        drawing.add(
            String(
                12, 58, "区间内无有效观测", fontName=font, fontSize=10, fillColor=MUTED
            )
        )
        return drawing
    start, end = (
        date.fromisoformat(points[0]["date"]),
        date.fromisoformat(points[-1]["date"]),
    )
    span = max(1, (end - start).days)
    for value in (-1, 0, 1):
        y = 25 + (value + 1) * 40
        drawing.add(Line(35, y, 475, y, strokeColor=colors.HexColor("#dce7e1")))
        drawing.add(
            String(4, y - 3, str(value), fontName=font, fontSize=8, fillColor=MUTED)
        )
    for a, b in zip(points, points[1:]):
        da, db = date.fromisoformat(a["date"]), date.fromisoformat(b["date"])
        if (db - da).days <= 35:
            drawing.add(
                Line(
                    35 + (da - start).days / span * 440,
                    25 + (a["ndvi"] + 1) * 40,
                    35 + (db - start).days / span * 440,
                    25 + (b["ndvi"] + 1) * 40,
                    strokeColor=GREEN,
                    strokeWidth=1.8,
                )
            )
    for point in points:
        x = 35 + (date.fromisoformat(point["date"]) - start).days / span * 440
        drawing.add(
            Rect(
                x - 1.5,
                25 + (point["ndvi"] + 1) * 40 - 1.5,
                3,
                3,
                fillColor=GREEN,
                strokeColor=None,
            )
        )
    drawing.add(
        String(35, 7, start.isoformat(), fontName=font, fontSize=8, fillColor=MUTED)
    )
    drawing.add(
        String(407, 7, end.isoformat(), fontName=font, fontSize=8, fillColor=MUTED)
    )
    return drawing


def spatial_map(item, font):
    drawing = Drawing(230, 145)
    spatial = item.get("spatial")
    if not spatial or not spatial["pixels"]:
        drawing.add(
            String(
                12, 64, "无可用空间像元", fontName=font, fontSize=10, fillColor=MUTED
            )
        )
        return drawing
    pixels = spatial["pixels"]
    xs, ys = [p[0] for p in pixels], [p[1] for p in pixels]
    dx, dy = max(max(xs) - min(xs), 0.0001), max(max(ys) - min(ys), 0.0001)
    scale = min(205 / dx, 105 / dy)
    size = max(1.5, min(5, 100 / max(1, len(pixels) ** 0.5)))
    for x, y, value in pixels:
        color = (
            "#b34a3c"
            if value < 0.25
            else "#d89e4c"
            if value < 0.35
            else "#b7ca71"
            if value < 0.5
            else "#238867"
        )
        drawing.add(
            Rect(
                10 + (x - min(xs)) * scale,
                25 + (y - min(ys)) * scale,
                size,
                size,
                fillColor=colors.HexColor(color),
                strokeColor=None,
            )
        )
    drawing.add(
        String(
            10,
            8,
            f"NDVI 空间分布 / {spatial['date']}",
            fontName=font,
            fontSize=8,
            fillColor=MUTED,
        )
    )
    return drawing


def render_report(snapshot: dict) -> bytes:
    """不查询数据库与网络，确保重新下载同一快照不会引入近期监测结论。"""
    if snapshot["request"]["mode"] != "historical":
        raise ValueError("近期分析不能导出为历史报告")
    font = font_name()
    styles = {
        "body": ParagraphStyle(
            "body",
            fontName=font,
            fontSize=9,
            leading=15,
            textColor=INK,
            wordWrap="CJK",
            spaceAfter=6,
        ),
        "title": ParagraphStyle(
            "title",
            fontName=font,
            fontSize=22,
            leading=30,
            textColor=INK,
            spaceAfter=12,
        ),
        "heading": ParagraphStyle(
            "heading",
            fontName=font,
            fontSize=13,
            leading=20,
            textColor=GREEN,
            spaceBefore=15,
            spaceAfter=8,
            keepWithNext=True,
        ),
        "small": ParagraphStyle(
            "small",
            fontName=font,
            fontSize=8,
            leading=12,
            textColor=MUTED,
            wordWrap="CJK",
            spaceAfter=5,
        ),
    }

    def p(value, style="body"):
        # 所有用户录入均作为纯文本，避免报告标题、农事备注注入 ReportLab 标记。
        return Paragraph(escape(str(value)), styles[style])

    def table(rows, widths):
        result = Table(
            [[p(cell, "small") for cell in row] for row in rows],
            colWidths=widths,
            repeatRows=1,
            hAlign="LEFT",
        )
        result.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e7f2ed")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#dce7e1")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        return result

    req = snapshot["request"]
    story = [
        p(req["brand_name"], "small"),
        p(req["title"], "title"),
        p(f"历史分析区间：{req['start_date']} 至 {req['end_date']}"),
        p(
            f"快照：{snapshot.get('snapshot_id') or '验证样例'} · 生成时间：{snapshot.get('created_at', '')}",
            "small",
        ),
        p(snapshot["data_note"], "small"),
        p("地块对比概览", "heading"),
    ]
    rows = [["地块 / 作物", "有效观测", "观测均值 NDVI", "期末观测日", "期末遥感表现"]]
    for item in snapshot["items"]:
        s = item["summary"]
        rows.append(
            [
                f"{item['land_name']} / {item.get('crop') or '未登记'}",
                str(s["count"]),
                shown(s["mean_ndvi"]),
                s["last_date"] or "暂无",
                PROGRESS[item["progress"]],
            ]
        )
    story.extend(
        [
            table(rows, [150, 58, 90, 95, 105]),
            p(
                "观测均值按有效观测日统计，观测密度不同会影响均值，不能直接用于地力或产量排名。",
                "small",
            ),
        ]
    )
    comparison = snapshot["comparison"]
    story.append(
        p(
            f"同步对比：匹配 {comparison['count']} 组观测（日期差不超过 3 天）。"
            + ("不足 3 组，不计算差值。" if comparison["status"] != "matched" else "")
        )
    )
    if comparison["status"] == "matched":
        story.append(
            table(
                [["地块", "匹配日 NDVI 均值", "相对首块差值"]]
                + [
                    [
                        item["land_name"],
                        shown(comparison["means"].get(item["land_id"])),
                        shown(comparison["differences"].get(item["land_id"])),
                    ]
                    for item in snapshot["items"]
                ],
                [240, 129, 129],
            )
        )
    story.append(p(comparison["note"], "small"))
    if not comparison["comparable_crop"]:
        story.append(p("所选地块作物不同或未登记，仅展示差异，不评判优劣。", "small"))
    for item in snapshot["items"]:
        story.extend(
            [p(item["land_name"] + " · 历史体检", "heading"), curve(item, font)]
        )
        spatial = item.get("spatial")
        if spatial:
            story.append(
                Table(
                    [
                        [
                            spatial_map(item, font),
                            p(
                                f"低绿度有效像元占比：{spatial['low_green_pct']}%；NDVI 离散程度：{spatial['ndvi_stddev']}。有效像元 {spatial['valid_pixels']} / 已存像元 {spatial['total_pixels']}。{spatial['note']}"
                            ),
                        ]
                    ],
                    colWidths=[235, 263],
                    style=[("VALIGN", (0, 0), (-1, -1), "MIDDLE")],
                )
            )
        story.append(
            p(
                "生育窗来源："
                + (
                    "人工确认"
                    if item["season_source"] == "manual"
                    else "历史影像自动推断"
                )
            )
        )
        if not item["effective_windows"]:
            story.append(p(item["phenology"]["note"], "small"))
        for window in item["effective_windows"]:
            story.append(
                p(
                    f"起点：{window.get('start_date') or '未覆盖'}；终点：{window.get('end_date') or '尚未确认'}；观测峰值：{window.get('peak_date') or '未指定'}；可信程度：{'人工确认' if window['confidence'] == 'user' else '中' if window['confidence'] == 'medium' else '低'}。"
                )
            )
        story.append(
            p("上述窗口反映冠层绿度起伏，不是精确播种、成熟或收获日。", "small")
        )
        for harvest in item["harvests"]:
            if harvest["status"] == "detected":
                story.append(
                    p(
                        f"收获候选观测：{harvest['harvest_date']}，该日呈收获后表现；{'低置信度，需复核' if harvest['confidence'] == 'low' else '需结合现场记录确认'}。"
                    )
                )
        rain = item["rainfall"]
        story.append(
            p(
                f"区间降雨记录合计：{shown(rain['mm'], 1)} mm；有记录 {rain['observed_days']} / {rain['period_days']} 天。缺测不计为零。",
                "small",
            )
        )
        if item["history"]:
            history = item["history"]
            c = history["comparison"]
            story.append(
                p(
                    f"历史同期：{history['start_date']} 至 {history['end_date']}；匹配 {c['count']} 组。所选期均值 {shown(c['means'].get('selected'))}，历史期均值 {shown(c['means'].get('reference'))}。"
                )
            )
            story.append(p(history["note"], "small"))
        for note in item["notes"]:
            story.append(p("核查提示：" + note))
    if snapshot["events"]:
        story.append(p("农服措施前后复盘", "heading"))
        names = {item["land_id"]: item["land_name"] for item in snapshot["items"]}
        for event in snapshot["events"]:
            story.append(
                p(f"{names[event['land_id']]} / {event['date']} / {event['action']}")
            )
            story.append(
                p(
                    f"前后各 {event['window_days']} 天；有效观测 {event['before']['count']} / {event['after']['count']} 个；NDVI 均值变化 {shown(event['change'])}；相对对照地块变化 {shown(event['relative_change'])}。"
                )
            )
            if event["note"]:
                story.append(p("用户备注：" + event["note"]))
            story.append(p(event["note_on_method"], "small"))
    story.extend(
        [
            Spacer(1, 12),
            p("阅读说明", "heading"),
            p(
                "空间图使用统一分级：低于 0.25、0.25 至 0.35、0.35 至 0.50、0.50 及以上。小地块和边界混合像元需谨慎解释。自动推断为待本地样本校准的经验方法；所有结论供营销说明和现场核查参考。",
                "small",
            ),
        ]
    )
    output = BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=48,
        leftMargin=48,
        topMargin=42,
        bottomMargin=42,
        title=req["title"],
        author=req["brand_name"],
    )

    def footer(canvas, _doc):
        canvas.setFont(font, 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(48, 25, "历史快照 · 不包含当前告警与实时监测")
        canvas.drawRightString(A4[0] - 48, 25, str(_doc.page))

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
