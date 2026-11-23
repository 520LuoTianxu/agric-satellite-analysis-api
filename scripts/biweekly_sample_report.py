#!/usr/bin/env python3
"""Generate biweekly report sample PDFs from reproducible demo inputs.

示例数据，非实测，不用于农事决策。

一条命令（在仓库根目录执行）：

    python scripts/biweekly_sample_report.py --out /tmp/biweekly-samples

每个场景会写出：
    <out>/<case>/inputs/products/*.json   示例像元产品（lonlat_columns_v1）
    <out>/<case>/inputs/facts.json        接口侧事实（TriggerService 构建）
    <out>/<case>/facts-enriched.json      下载机侧补充后的事实
    <out>/<case>/figures/*.png            报告内图件
    <out>/<case>/biweekly-demo-<case>.pdf 报告样稿
    <out>/<case>/pages/page-NN.png        逐页图片（加 --pages，需安装 pymupdf）

脚本只在本机读写文件：不访问数据库、Redis、对象存储和内部接口，不下载影像和气象数据，
也不调用 Celery 任务。报告由正式入口 generate_official_biweekly_pdf 生成。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "services" / "ingest", ROOT / "packages" / "agric_satellite_analysis_common"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


def export_pages(pdf: Path, dest: Path, *, dpi: int = 110) -> list[Path]:
    """Render every page to PNG for visual checks. Needs pymupdf."""
    try:
        import pymupdf
    except ImportError:  # 旧版 pymupdf 只有 fitz 这个模块名
        import fitz as pymupdf

    dest.mkdir(parents=True, exist_ok=True)
    pages = []
    with pymupdf.open(str(pdf)) as doc:
        for index, page in enumerate(doc, start=1):
            target = dest / f"page-{index:02d}.png"
            page.get_pixmap(dpi=dpi).save(str(target))
            pages.append(target)
    return pages


def main(argv: list[str] | None = None) -> int:
    from app.reports.biweekly.demo_data import CASES, render_case

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", default="/tmp/biweekly-samples", help="输出目录")
    parser.add_argument("--case", action="append", choices=sorted(CASES), help="只生成指定场景，可重复；默认全部")
    parser.add_argument("--pages", action="store_true", help="同时把每页导出为 PNG（需安装 pymupdf）")
    parser.add_argument("--dpi", type=int, default=110, help="逐页 PNG 的分辨率")
    args = parser.parse_args(argv)

    out = Path(args.out)
    for name in args.case or sorted(CASES):
        built = render_case(name, out)
        print(f"{name}: {built['pdf']}  ({CASES[name]['title']}，示例数据，非实测)")
        if args.pages:
            try:
                pages = export_pages(built["pdf"], built["out_dir"] / "pages", dpi=args.dpi)
            except ImportError:
                print("  未安装 pymupdf，跳过逐页导出（pip install pymupdf）", file=sys.stderr)
            else:
                print(f"  逐页图片 {len(pages)} 张：{pages[0].parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
