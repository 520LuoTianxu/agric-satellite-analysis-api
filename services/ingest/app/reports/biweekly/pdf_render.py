"""Detailed local PDF from the SAME immutable facts used by the raw page.

Reuse existing ReportLab dependency and packaged WenQuanYi font. Missing font is
an explicit failure, never silently replaced by a font that drops Chinese text.
"""

from __future__ import annotations

import io
from datetime import date, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from agric_satellite_analysis_common.biweekly.models import (
    Narrative,
    RenderedPdf,
    ReportError,
    Snapshot,
)


class BiweeklyPdfRenderer:
    template_version = "biweekly-pdf-offline-v1"

    def __init__(self, *, font_path: Path | None = None):
        self.font_path = (
            Path(font_path)
            if font_path
            else Path(__file__).resolve().parents[1]
            / "land_assessment/fonts/wqy-zenhei.ttf"
        )

    def render(self, snapshot: Snapshot, narrative: Narrative) -> RenderedPdf:
        if not self.font_path.is_file():
            raise ReportError("FONT_UNAVAILABLE")
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )

        font = "BiweeklyOfflineCN"
        try:
            pdfmetrics.registerFont(TTFont(font, str(self.font_path)))
        except Exception:
            raise ReportError("FONT_UNAVAILABLE") from None
        ink, green, muted = [
            colors.HexColor(c) for c in ("#294638", "#2e7853", "#809589")
        ]
        styles = {
            "body": ParagraphStyle(
                "body",
                fontName=font,
                fontSize=10,
                leading=18,
                spaceAfter=8,
                textColor=ink,
                wordWrap="CJK",
            ),
            "title": ParagraphStyle(
                "title",
                fontName=font,
                fontSize=25,
                leading=35,
                spaceAfter=12,
                textColor=green,
                wordWrap="CJK",
            ),
            "h": ParagraphStyle(
                "h",
                fontName=font,
                fontSize=14,
                leading=23,
                spaceBefore=12,
                spaceAfter=9,
                textColor=green,
                wordWrap="CJK",
            ),
            "small": ParagraphStyle(
                "small",
                fontName=font,
                fontSize=8,
                leading=13,
                spaceAfter=5,
                textColor=muted,
                wordWrap="CJK",
            ),
        }

        def p(value, style="body"):
            return Paragraph(escape(str(value)).replace("\n", "<br/>"), styles[style])

        def table(rows, widths):
            value = Table(
                [[p(cell, "small") for cell in row] for row in rows],
                colWidths=widths,
                repeatRows=1,
                hAlign="LEFT",
            )
            value.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf4ef")),
                        (
                            "LINEBELOW",
                            (0, 0),
                            (-1, -1),
                            0.4,
                            colors.HexColor("#dce8df"),
                        ),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
            return value

        data = snapshot.data
        if data["data_kind"] != "mock":
            raise ReportError("OFFLINE_MOCK_ONLY")
        end_day = date.fromisoformat(data["window"]["end"]) - timedelta(days=1)
        story = [
            p("种植双周报", "title"),
            p("离线逻辑演示 / MOCK / 不是真实地块报告", "h"),
            p(f"{data['crop']} · {data['field_id']} · {data['season_id']}", "small"),
            p(f"{data['window']['start']} — {end_day}（Asia/Shanghai）", "small"),
            p(
                "计划BTU窗口不是实测物候；所有输入为合成示例。网页原始数据与本PDF使用同一事实快照。未校准FVC、未批准LAI模型不能作为可靠反演结果。"
            ),
            p("计划阶段与跨期归属", "h"),
        ]
        story.append(p("主归属：" + (data["primary_period_id"] or "缺失，未推断")))
        rows = [["计划生育期", "农艺阶段", "覆盖窗口（结束日不含）", "天数"]]
        for period in data["covered_periods"]:
            rows.append(
                [
                    period["name"],
                    period["phase"],
                    period["covered_start"] + " — " + period["covered_end"],
                    period["days"],
                ]
            )
        if len(rows) == 1:
            rows.append(["暂无计划数据", "未知", "未补造排期", "—"])
        story.extend(
            [
                table(rows, [120, 70, 260, 60]),
                p(
                    "跨期报告按起始日唯一归属，覆盖期为解释注记，不复制为多份报告。S标签为农艺阶段，不是卫星传感器。",
                    "small",
                ),
                p("结构化证据与天气", "h"),
            ]
        )
        evidence_rows = [["证据ID / 指标", "值 / 单位", "可用性"]]
        for evidence in data["evidence"]:
            fact = data["facts"].get(evidence["id"])
            value = (
                f"{fact['value']} {fact['unit']}"
                if fact
                else "缺失或未验证，不显示数值"
            )
            evidence_rows.append(
                [
                    evidence["id"] + " / " + evidence["label"],
                    value,
                    evidence["availability"],
                ]
            )
        if len(evidence_rows) == 1:
            evidence_rows.append(["无可用证据", "未补零", "missing"])
        story.extend(
            [
                table(evidence_rows, [220, 180, 110]),
                p(
                    "有效观测日：{valid_days}；有效像元比例：{valid_fraction}；最大间隔天数：{max_gap_days}。均为输入适配器提供的模拟质量字段。".format(
                        **data["sampling"]
                    ),
                    "small",
                ),
                p("阶段化解读与核查", "h"),
                p(
                    "未调用AI；无模型模板文案。"
                    if narrative.mode == "template"
                    else "注入适配器文案，仅离线演示，需人工审核。",
                    "small",
                ),
                p(narrative.data["summary"]),
            ]
        )
        for claim in narrative.data["claims"]:
            story.append(p(claim["text"]))
            story.append(p("证据引用：" + "、".join(claim["evidence_refs"]), "small"))
            for fact in claim["values"]:
                story.append(
                    p(
                        f"{fact['evidence_id']} = {fact['value']} {fact['unit']}",
                        "small",
                    )
                )
        story.append(p("质量限制与缺失", "h"))
        for flag in data["quality_flags"]:
            story.append(p(flag, "small"))
        story.extend(
            [
                p(
                    "缺少灌溉、田间墒情及完整Sentinel-1雷达等灾害证据，不作受旱/受涝、产量或损失定性。FVC无地面校准样本，LAI缺已批准模型与输入，不通过NDVI补造。"
                ),
                p("版本、输入指纹与复现", "h"),
                table(
                    [
                        ["属性", "版本 / 标记"],
                        [
                            "snapshot_sha256",
                            snapshot.sha256[:32] + "\n" + snapshot.sha256[32:],
                        ],
                        [
                            "source_revision / plan",
                            data["source_revision"] + " / " + data["plan_version"],
                        ],
                        [
                            "policy / mapping",
                            data["policy_version"] + " / " + data["mapping_version"],
                        ],
                        ["template", self.template_version],
                        [
                            "model / prompt",
                            narrative.model_version + " / " + narrative.prompt_version,
                        ],
                    ],
                    [145, 365],
                ),
                Spacer(1, 12),
                p(
                    "后台尚未对接生产源、真实模型或前端。保留原始限制，禁止将此演示当作农学处方。",
                    "small",
                ),
            ]
        )
        stream = io.BytesIO()
        doc = SimpleDocTemplate(
            stream,
            pagesize=A4,
            leftMargin=42,
            rightMargin=42,
            topMargin=48,
            bottomMargin=45,
            title="MOCK 种植双周报",
            author="biweekly-offline",
            invariant=1,
        )

        def footer(canvas, document):
            canvas.saveState()
            canvas.setFont(font, 8)
            canvas.setFillColor(muted)
            canvas.drawString(42, 25, "合成数据 / MOCK / 非真实农情结论")
            canvas.drawRightString(A4[0] - 42, 25, str(document.page))
            canvas.restoreState()

        try:
            doc.build(story, onFirstPage=footer, onLaterPages=footer)
        except Exception:
            raise ReportError("PDF_RENDER_FAILED") from None
        return RenderedPdf(stream.getvalue(), doc.page)
