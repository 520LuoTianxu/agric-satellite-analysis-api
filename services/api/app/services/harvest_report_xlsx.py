"""收获进度报表 Excel（xlsxwriter constant_memory 流式写）：汇总 / 逐日明细 / 透视 / 说明 四个工作表。

前三张表最左侧为 省/租户/项目/地块 四个独立列，并按四级行分组（outline）下钻；
汇总行只填到本级，下一级列写“小计”。

constant_memory 模式按行顺序流式落盘，每写完一行即释放，内存与行数基本无关
（openpyxl 全量驻留单元格对象，2 万地块时子进程峰值约 3GB）。因此每行的所有样式
（进度分档底色、沿用值斜体等）都在写该行时一次确定，不能事后回改单元格。
"""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from typing import Any

import xlsxwriter

from app.services.harvest_report import (
    BUCKET_LABELS_ZH,
    BUCKETS,
    CONFIDENCE_LABELS_ZH,
    STATUS_LABELS_ZH,
    ReportFilters,
)

CN_TZ = timezone(timedelta(hours=8))

HEADER_BG, HEADER_FG = "#1F6F43", "#FFFFFF"
KPI_LABEL_BG = "#E8F3EC"
BORDER_COLOR = "#D1D5DB"
BUCKET_FILLS = {
    "none": "F3F4F6",
    "lt30": "FEF3C7",
    "30_90": "FDBA74",
    "ge90": "C2410C",
    "not_computed": "E5E7EB",
}

PCT = '0.0"%"'
MU = "#,##0.00"
DATE = "yyyy-mm-dd"

# 四级下钻：省级 → 租户级 → 项目级 → 地块级
LEVELS: list[tuple[str, str, str]] = [
    ("province_name", "省", "未知省份"),
    ("base_name", "租户", "未知租户"),
    ("group_name", "项目", "未分组项目"),
]
LEVEL_FILLS = ["CFE8D8", "E1F1E6", "F1F8F3"]
# (字号, 颜色)，均加粗
LEVEL_FONTS = [(12, "14532D"), (11, "166534"), (10, "15803D")]
BASE_FONT_NAME = "微软雅黑"

# (key, 表头, 列宽, 数字格式)
SUMMARY_COLS: list[tuple[str, str, int, str | None]] = [
    ("province_name", "省", 14, None),
    ("base_name", "租户", 18, None),
    ("group_name", "项目", 18, None),
    ("land_name", "地块", 20, None),
    ("land_id", "地块ID", 12, None),
    ("land_count", "地块数", 8, "#,##0"),
    ("computed_count", "已计算地块", 9, "#,##0"),
    ("crop_type", "作物", 8, None),
    ("city_name", "市", 10, None),
    ("county_name", "区县", 10, None),
    ("area_mu", "面积(亩)", 12, MU),
    ("harvested_pct", "已收获%", 10, PCT),
    ("suspected_pct", "疑似收获%", 10, PCT),
    ("combined_pct", "合计%(已收获+疑似)", 13, PCT),
    ("harvested_area_mu", "已收获面积(亩)", 13, MU),
    ("combined_area_mu", "合计收获面积(亩)", 14, MU),
    ("newly_combined_pct", "较上期新增%", 11, PCT),
    ("status_zh", "状态", 9, None),
    ("bucket_zh", "进度分档", 14, None),
    ("season_start", "本季起始日", 12, DATE),
    ("first_harvest_date", "首次收获日", 12, DATE),
    ("latest_date", "最新结果日", 12, DATE),
    ("latest_obs_date", "最新影像日", 12, DATE),
    ("days_since_last_image", "距最新影像(天)", 10, "0"),
    ("confidence", "置信度", 8, "0.00"),
    ("confidence_zh", "置信等级", 8, None),
    ("confirmed_zh", "已确认", 7, None),
    ("obs_count", "区间结果期数", 9, "0"),
]

DAILY_COLS: list[tuple[str, str, int, str | None]] = [
    ("province_name", "省", 14, None),
    ("base_name", "租户", 18, None),
    ("group_name", "项目", 18, None),
    ("land_name", "地块", 20, None),
    ("land_id", "地块ID", 10, None),
    ("land_count", "地块数", 8, "#,##0"),
    ("record_count", "记录数", 8, "#,##0"),
    ("date", "观测日", 12, DATE),
    ("crop_type", "作物", 8, None),
    ("county_name", "区县", 10, None),
    ("area_mu", "面积(亩)", 12, MU),
    ("status_zh", "状态", 9, None),
    ("harvested_pct", "已收获%", 10, PCT),
    ("suspected_pct", "疑似收获%", 10, PCT),
    ("combined_pct", "合计%", 10, PCT),
    ("newly_harvested_pct", "新增已收获%", 11, PCT),
    ("newly_combined_pct", "较上期新增合计%", 13, PCT),
    ("harvested_area_mu", "已收获面积(亩)", 13, MU),
    ("combined_area_mu", "合计收获面积(亩)", 14, MU),
    ("valid_pct", "有效像元%", 10, PCT),
    ("season_start", "本季起始日", 12, DATE),
    ("confidence", "置信度", 8, "0.00"),
    ("confidence_zh", "置信等级", 8, None),
    ("confirmed_zh", "已确认", 7, None),
]
LEVEL_ZH = ["省", "租户", "项目", "地块"]
HIER_KEYS = ["province_name", "base_name", "group_name", "land_name"]
N_HIER = len(HIER_KEYS)


def _hier(path: tuple[str, ...], level: int, land_name: Any = None) -> dict[str, Any]:
    """层级列取值：明细行四列填满；汇总行只填到本级，下一级写“小计”，其余留空。"""
    out: dict[str, Any] = {k: None for k in HIER_KEYS}
    for i, v in enumerate(path[:3]):
        out[HIER_KEYS[i]] = v
    if level < 3:
        out[HIER_KEYS[level + 1]] = "小计"
    else:
        out["land_name"] = land_name
    return out


def _decorate(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    out["status_zh"] = STATUS_LABELS_ZH.get(
        r.get("status") or "", r.get("status") or ""
    )
    if "bucket" in r:
        out["bucket_zh"] = BUCKET_LABELS_ZH.get(r["bucket"], r["bucket"])
    out["confidence_zh"] = CONFIDENCE_LABELS_ZH.get(r.get("confidence_level") or "", "")
    c = r.get("confirmed")
    out["confirmed_zh"] = "" if c is None else ("是" if c else "否（暂定）")
    return out


def _level_key(rec: dict[str, Any], depth: int) -> tuple[str, ...]:
    out = []
    for key, _zh, missing in LEVELS[:depth]:
        v = rec.get(key)
        out.append(str(v).strip() if v not in (None, "") else missing)
    return tuple(out)


def _tree(records: list[dict[str, Any]]):
    """按 省→租户→项目 分组，返回 [(level, path, members)] 前序遍历（组行在前）。"""
    ordered = sorted(
        records,
        key=lambda r: (
            _level_key(r, 3),
            str(r.get("land_name") or ""),
            str(r.get("land_id") or ""),
            r.get("date") or date.min,
        ),
    )
    out: list[tuple[int, tuple[str, ...], list[dict[str, Any]]]] = []

    def walk(level: int, prefix: tuple[str, ...], members: list[dict[str, Any]]):
        if level == 3:
            return
        groups: dict[str, list[dict[str, Any]]] = {}
        for m in members:
            groups.setdefault(_level_key(m, level + 1)[level], []).append(m)
        for name, sub in groups.items():
            out.append((level, prefix + (name,), sub))
            walk(level + 1, prefix + (name,), sub)

    walk(0, (), ordered)
    return out


def _weighted(items: list[tuple[float | None, float | None]]) -> float | None:
    num = den = 0.0
    for area, pct in items:
        if pct is None:
            continue
        a = float(area) if area else 0.0
        num += a * float(pct)
        den += a
    if den > 0:
        return round(num / den, 1)
    vals = [float(p) for _a, p in items if p is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _agg(parcels: list[dict[str, Any]]) -> dict[str, Any]:
    computed = [p for p in parcels if p.get("bucket") != "not_computed"]

    def s(key: str, rows) -> float | None:
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        return round(sum(vals), 2) if vals else None

    return {
        "land_count": len(parcels),
        "computed_count": len(computed),
        "area_mu": s("area_mu", parcels),
        "harvested_area_mu": s("harvested_area_mu", computed),
        "combined_area_mu": s("combined_area_mu", computed),
        "harvested_pct": _weighted([(p.get("area_mu"), p.get("harvested_pct")) for p in computed]),
        "suspected_pct": _weighted([(p.get("area_mu"), p.get("suspected_pct")) for p in computed]),
        "combined_pct": _weighted([(p.get("area_mu"), p.get("combined_pct")) for p in computed]),
    }


def _filters_text(f: ReportFilters) -> list[tuple[str, str]]:
    return [
        ("日期区间", f"{f.date_from.isoformat()} ~ {f.date_to.isoformat()}"),
        ("分组ID", f.group_id or "全部"),
        ("作物", f.crop or "全部"),
        (
            "进度分档",
            "、".join(BUCKET_LABELS_ZH.get(b, b) for b in f.buckets) or "全部",
        ),
        ("最低合计%", "" if f.min_pct is None else f"{f.min_pct:g}"),
        ("关键字", f.keyword or ""),
    ]


def _notes(gen: datetime, filters: ReportFilters, kpi: dict[str, Any], carry_forward: bool):
    lines: list[tuple[str, str]] = [
        ("生成时间", f"{gen:%Y-%m-%d %H:%M:%S} (UTC+8)"),
        *_filters_text(filters),
        ("", ""),
        (
            "算法",
            "Sentinel-2 残茬/裸土单调判别 + Sentinel-1 佐证："
            "每像元在本季内状态单调（未收获→疑似收获→已收获），占比只增不减。",
        ),
        (
            "落库门槛",
            f"只有合计占比 > {kpi.get('min_save_pct', 3):g}% 的观测日入库；"
            "区间内没有结果行但已计算区间标记覆盖查询区间的地块，记为“未收获（≤门槛）”，合计 0%。",
        ),
        (
            "未计算",
            "既无结果行也无已计算区间标记的地块（尚未运行 harvest-progress 计算/回填），"
            "不计入平均值与收获面积；报表不做现场计算。",
        ),
        (
            "透视表",
            "单元格为该观测日合计%（已收获+疑似）；省/租户/项目汇总行为面积加权值。"
            + (
                "斜体为沿用上一期结果（结果单调，未入库日沿用上一期值，仅限同一季）；"
                "首个结果日之前留空（≤门槛）。"
                if carry_forward
                else "仅显示入库观测日，未入库日留空。"
            ),
        ),
        ("", ""),
        (
            "下钻",
            "汇总/逐日明细/透视三张表均按 省 → 租户 → 项目 → 地块 四级分组，省/租户/项目/地块各占一列（明细行四列完整，便于筛选；汇总行只填到本级，下一级写“小计”）；"
            "点击左侧 1/2/3/4 或 +/- 可折叠展开。汇总行的百分比为已计算地块的面积加权值，面积为合计。",
        ),
        ("", ""),
        ("字段", "定义"),
        ("已收获%", "高确定性已收获像元占作物像元比例（单调）。"),
        (
            "疑似收获%",
            "疑似收获像元占比（出现残茬/突变特征但尚未确认）；后续确认即转为已收获。",
        ),
        ("合计%", "已收获% + 疑似收获%（单调）；进度分档与“最低合计%”筛选均按此值。"),
        ("已收获面积(亩)", "面积 × 已收获%。"),
        ("合计收获面积(亩)", "面积 × 合计%。"),
        (
            "较上期新增%",
            "合计% 相对同一季上一条结果行的增量；无上一条时即本期合计（此前均 ≤ 门槛）。",
        ),
        ("新增已收获%", "算法输出的已收获% 较上一观测日新增。"),
        ("首次收获日", "本季第一个合计% 超过落库门槛的观测日。"),
        ("最新结果日", "查询区间内最后一个入库观测日；地块最新状态取自该日。"),
        (
            "最新影像日 / 距最新影像(天)",
            "截至区间末日最近一期 S2 影像日期，以及到 min(区间末日, 今天) 的天数。",
        ),
        ("状态", "生长中 / 收获中 / 已收获（算法状态）。"),
        ("进度分档", "未收获（≤门槛）/ 收获 <30% / 收获 30–90% / 收获 ≥90% / 未计算。"),
        (
            "置信度 / 置信等级",
            "0–1 分值与 高/中/低 等级（像元数、观测间隔、S1 一致性等）。",
        ),
        ("已确认", "否（暂定）表示最近观测尚未被后续观测确认，后续可能修正。"),
        ("区间结果期数", "查询区间内入库观测日数。"),
    ]
    return lines


class _Styles:
    """xlsxwriter Format 缓存：同样式组合只建一个 Format（样式表小、无逐格哈希）。"""

    def __init__(self, wb: xlsxwriter.Workbook):
        self.wb = wb
        self._cache: dict[tuple, Any] = {}

    def get(self, **props: Any):
        key = tuple(sorted(props.items()))
        f = self._cache.get(key)
        if f is None:
            f = self.wb.add_format(props)
            self._cache[key] = f
        return f

    def cell(
        self,
        level: int,
        num_fmt: str | None = None,
        align: str | None = None,
        subtotal: bool = False,
        bucket: str | None = None,
        italic: bool = False,
    ):
        """level 0-2 汇总行（底色+粗体），3 明细行；与原 openpyxl 版逐项一致。"""
        p: dict[str, Any] = {"border": 1, "border_color": BORDER_COLOR}
        if level < 3:
            size, color = LEVEL_FONTS[level]
            p.update(bold=True, font_size=size, font_color="#" + color,
                     bg_color="#" + LEVEL_FILLS[level], pattern=1)
        else:
            p.update(font_name=BASE_FONT_NAME, font_size=10)
        if num_fmt:
            p["num_format"] = num_fmt
        if align:
            p.update(align=align, valign="vcenter")
        if subtotal:
            p.pop("font_name", None)
            p.update(bold=True, italic=True, font_size=10, font_color="#6B7280")
        if bucket and bucket in BUCKET_FILLS:
            p.update(bg_color="#" + BUCKET_FILLS[bucket], pattern=1)
            if bucket == "ge90":
                p.pop("font_name", None)
                p.update(font_color="#FFFFFF", bold=True, font_size=11)
        if italic:
            p["italic"] = True
        return self.get(**p)

    def header(self, rotate: bool = False, num_fmt: str | None = None):
        p: dict[str, Any] = dict(bold=True, font_color=HEADER_FG, bg_color=HEADER_BG, pattern=1,
                                 border=1, border_color=BORDER_COLOR, align="center")
        if rotate:
            p["rotation"] = 90
        else:
            p.update(valign="vcenter", text_wrap=True)
        if num_fmt:
            p["num_format"] = num_fmt
        return self.get(**p)


def _write_value(ws, r: int, c: int, v: Any, fmt) -> None:
    if v is None or v == "":
        ws.write_blank(r, c, None, fmt)
    elif isinstance(v, bool):
        ws.write_boolean(r, c, v, fmt)
    elif isinstance(v, (datetime, date)):
        ws.write_datetime(r, c, v, fmt)
    elif isinstance(v, (int, float)):
        ws.write_number(r, c, v, fmt)
    elif isinstance(v, str):
        ws.write_string(r, c, v, fmt)
    else:
        ws.write(r, c, v, fmt)


def _setup_sheet(ws, cols_widths: list[int]) -> None:
    """汇总行在明细上方；左侧 1/2/3/4 按钮一键折叠到省/租户/项目/地块。打印：横向、按页宽缩放。"""
    for i, w in enumerate(cols_widths):
        ws.set_column(i, i, w)
    ws.outline_settings(True, False, True, False)  # visible, symbols_below=False, right, auto_style
    ws.hide_gridlines(2)
    ws.set_zoom(90)
    ws.set_landscape()
    ws.fit_to_pages(1, 0)


def _header(ws, st: _Styles, r: int, cols) -> None:
    ws.set_row(r, 32)
    f = st.header()
    for i, (_k, title, _w, _fmt) in enumerate(cols):
        ws.write_string(r, i, title, f)


def _write_row(ws, st: _Styles, r: int, cols, rec: dict[str, Any], level: int,
               bucket_col: int | None = None, bucket: str | None = None,
               italic_cols: set[int] | None = None) -> None:
    """r 为 0 基行号。level 0-2 汇总行，3 明细行；行分组 outline 层级 = level。"""
    group_row = level < 3
    height = 20 - level if group_row else None
    if level or height:
        ws.set_row(r, height, None, {"level": level} if level else None)
    for i, (k, _t, _w, fmt) in enumerate(cols):
        v = rec.get(k)
        align = None
        if i < N_HIER:
            align = "left"
        elif isinstance(v, str):
            align = "center" if k.endswith("_zh") else "left"
        f = st.cell(
            level,
            fmt if (fmt and v is not None) else None,
            align,
            i < N_HIER and v == "小计",
            bucket if i == bucket_col else None,
            bool(italic_cols and i in italic_cols),
        )
        _write_value(ws, r, i, v, f)


def _pct_bars(ws, cols, keys, first: int, last: int) -> None:
    if last < first:
        return
    for key, color in keys:
        c = [x[0] for x in cols].index(key)
        ws.conditional_format(first, c, last, c, {
            "type": "data_bar", "min_type": "num", "min_value": 0,
            "max_type": "num", "max_value": 100, "bar_color": "#" + color,
        })


def build_workbook_to_path(
    out_path: str,
    parcels: list[dict[str, Any]],
    daily: list[dict[str, Any]],
    kpi: dict[str, Any],
    filters: ReportFilters,
    generated_at: datetime | None = None,
    carry_forward: bool = True,
) -> int:
    """流式生成工作簿到 path，返回字节数。同步导出与异步导出共用。"""
    gen = (generated_at or datetime.now(CN_TZ)).astimezone(CN_TZ)
    wb = xlsxwriter.Workbook(out_path, {"constant_memory": True, "tmpdir": tempfile.gettempdir()})
    st = _Styles(wb)

    # ---- Sheet1 汇总 ----（0 基行号：标题 0、副标题 1、KPI 3/4、表头 6）
    ws = wb.add_worksheet("汇总")
    _setup_sheet(ws, [w for _k, _t, w, _f in SUMMARY_COLS])
    ws.write_string(0, 0, "地块收获进度汇总", st.get(bold=True, font_size=14))
    ws.write_string(
        1, 0,
        f"区间 {filters.date_from.isoformat()} ~ {filters.date_to.isoformat()}"
        f"　生成时间 {gen:%Y-%m-%d %H:%M} (UTC+8)",
        st.get(font_color="#6B7280"),
    )
    bc = kpi["bucket_counts"]
    kpis = [
        ("地块数", kpi["parcel_count"], "0"),
        ("已计算地块数", kpi["computed_count"], "0"),
        ("总面积(亩)", kpi["total_area_mu"], MU),
        ("已收获面积(亩)", kpi["harvested_area_mu"], MU),
        ("合计收获面积(亩)", kpi["combined_area_mu"], MU),
        ("平均合计%", kpi["avg_combined_pct"], PCT),
        ("面积加权合计%", kpi["area_weighted_combined_pct"], PCT),
    ] + [(BUCKET_LABELS_ZH[b], bc.get(b, 0), "0") for b in BUCKETS]
    bd = {"border": 1, "border_color": BORDER_COLOR}
    ws.set_row(3, 30)
    lab = st.get(bg_color=KPI_LABEL_BG, pattern=1, bold=True, font_size=9, align="center",
                 text_wrap=True, **bd)
    for i, (label, _v, _f) in enumerate(kpis):
        ws.write_string(3, i, label, lab)
    for i, (_l, value, fmt) in enumerate(kpis):
        _write_value(ws, 4, i, value,
                     st.get(bold=True, font_size=12, num_format=fmt, align="center", **bd))
    hdr = 6
    _header(ws, st, hdr, SUMMARY_COLS)
    bcol = [c[0] for c in SUMMARY_COLS].index("bucket_zh")
    r = hdr + 1
    for level, path, members in _tree(parcels):
        rec = {**_hier(path, level), **_agg(members)}
        _write_row(ws, st, r, SUMMARY_COLS, rec, level)
        r += 1
        if level == 2:
            for p in members:
                d = _decorate(p)
                d.update(_hier(path, 3, p.get("land_name") or p.get("land_id")), land_count=1,
                         computed_count=0 if p.get("bucket") == "not_computed" else 1)
                _write_row(ws, st, r, SUMMARY_COLS, d, 3, bcol, p.get("bucket"))
                r += 1
    last = r - 1
    ws.freeze_panes(hdr + 1, N_HIER)
    ws.autofilter(hdr, 0, max(last, hdr), len(SUMMARY_COLS) - 1)
    _pct_bars(ws, SUMMARY_COLS,
              [("harvested_pct", "C2410C"), ("suspected_pct", "FDBA74"), ("combined_pct", "EA580C")],
              hdr + 1, last)
    ws.repeat_rows(hdr)

    # ---- Sheet2 逐日明细 ----
    ws2 = wb.add_worksheet("逐日明细")
    _setup_sheet(ws2, [w for _k, _t, w, _f in DAILY_COLS])
    _header(ws2, st, 0, DAILY_COLS)
    r = 1
    for level, path, members in _tree(daily):
        rec = {
            **_hier(path, level),
            "land_count": len({m["land_id"] for m in members}),
            "record_count": len(members),
        }
        _write_row(ws2, st, r, DAILY_COLS, rec, level)
        r += 1
        if level == 2:
            for m in members:
                d = _decorate(m)
                d.update(_hier(path, 3, m.get("land_name") or m.get("land_id")))
                _write_row(ws2, st, r, DAILY_COLS, d, 3)
                r += 1
    last2 = r - 1
    ws2.freeze_panes(1, N_HIER)
    ws2.autofilter(0, 0, max(last2, 0), len(DAILY_COLS) - 1)
    _pct_bars(ws2, DAILY_COLS, [("combined_pct", "EA580C")], 1, last2)
    ws2.repeat_rows(0)

    # ---- Sheet3 透视 ----
    ws3 = wb.add_worksheet("透视")
    dates = sorted({x["date"] for x in daily if x["date"]})
    fixed = [
        *[(k, t, w) for k, t, w, _f in SUMMARY_COLS[:N_HIER]],
        ("land_id", "地块ID", 10),
        ("area_mu", "面积(亩)", 11),
    ]
    nf = len(fixed)
    _setup_sheet(ws3, [w for _k, _t, w in fixed] + [6] * len(dates))
    ws3.set_row(0, 48)
    for i, (_k, title, _w) in enumerate(fixed):
        ws3.write_string(0, i, title, st.header())
    for j, d in enumerate(dates):
        ws3.write_datetime(0, nf + j, d, st.header(rotate=True, num_fmt="mm-dd"))
    cell_of: dict[tuple[str, date], dict[str, Any]] = {
        (x["land_id"], x["date"]): x for x in daily if x["date"]
    }
    values: dict[str, list[tuple[Any, bool]]] = {}
    for p in parcels:
        carried: dict[str, Any] | None = None
        seq: list[tuple[Any, bool]] = []
        for d in dates:
            rr = cell_of.get((p["land_id"], d))
            if rr is not None:
                carried = rr
                seq.append((rr["combined_pct"], False))
            elif (
                carry_forward
                and carried is not None
                and (p["season_start"] is None or carried["season_start"] == p["season_start"])
            ):
                seq.append((carried["combined_pct"], True))
            else:
                seq.append((None, False))
        values[p["land_id"]] = seq
    del cell_of
    cols = [(k, t, w, MU if k == "area_mu" else None) for k, t, w in fixed] + [
        (f"d{j}", "", 6, "0") for j in range(len(dates))
    ]
    r = 1
    for level, path, members in _tree(parcels):
        rec = {**_hier(path, level), "area_mu": _agg(members)["area_mu"]}
        for j in range(len(dates)):
            rec[f"d{j}"] = _weighted(
                [(m.get("area_mu"), values[m["land_id"]][j][0]) for m in members]
            )
        _write_row(ws3, st, r, cols, rec, level)
        r += 1
        if level == 2:
            for p in members:
                seq = values[p["land_id"]]
                prec = {
                    **_hier(path, 3, p.get("land_name") or p["land_id"]),
                    "land_id": p["land_id"],
                    "area_mu": p.get("area_mu"),
                }
                for j, (v, _c) in enumerate(seq):
                    prec[f"d{j}"] = v
                ital = {nf + j for j, (_v, c) in enumerate(seq) if c}
                _write_row(ws3, st, r, cols, prec, 3, italic_cols=ital)
                r += 1
    last3 = r - 1
    if dates and last3 >= 1:
        ws3.conditional_format(1, nf, last3, nf + len(dates) - 1, {
            "type": "3_color_scale",
            "min_type": "num", "min_value": 0, "min_color": "#F0FDF4",
            "mid_type": "num", "mid_value": 50, "mid_color": "#FDBA74",
            "max_type": "num", "max_value": 100, "max_color": "#9A3412",
        })
        # 深色单元格用白字，保证沿用值（斜体）与实测值都可读
        ws3.conditional_format(1, nf, last3, nf + len(dates) - 1, {
            "type": "cell", "criteria": ">=", "value": 60,
            "format": wb.add_format({"font_color": "#FFFFFF"}),
        })
    ws3.freeze_panes(1, N_HIER)
    ws3.autofilter(0, 0, max(last3, 0), nf + len(dates) - 1)
    ws3.repeat_rows(0)

    # ---- Sheet4 说明 ----
    ws4 = wb.add_worksheet("说明")
    ws4.set_column(0, 0, 22)
    ws4.set_column(1, 1, 100)
    fa = st.get(bold=True)
    fb = st.get(text_wrap=True, valign="top")
    fh = st.get(bold=True, font_color=HEADER_FG, bg_color=HEADER_BG, pattern=1)
    for i, (k, v) in enumerate(_notes(gen, filters, kpi, carry_forward)):
        if k == "字段":
            ws4.write_string(i, 0, k, fh)
            ws4.write_string(i, 1, v, fh)
        else:
            _write_value(ws4, i, 0, k, fa)
            _write_value(ws4, i, 1, v, fb)

    wb.close()
    return os.path.getsize(out_path)


def build_workbook(
    parcels: list[dict[str, Any]],
    daily: list[dict[str, Any]],
    kpi: dict[str, Any],
    filters: ReportFilters,
    generated_at: datetime | None = None,
    carry_forward: bool = True,
) -> bytes:
    """同步导出用：生成到临时文件后读回字节。"""
    fd, path = tempfile.mkstemp(prefix="harvest-report-", suffix=".xlsx")
    os.close(fd)
    try:
        build_workbook_to_path(path, parcels, daily, kpi, filters, generated_at, carry_forward)
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        os.unlink(path)
