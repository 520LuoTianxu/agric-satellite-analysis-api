#!/usr/bin/env python3
"""Render official biweekly PDFs for one real land from exported files, fully offline.

用导出的真实数据文件，离线生成某个地块各期的正式种植双周报。

    # 1. 只看种植季与分期（不需要观测数据），并给出各类数据应导出的日期范围
    python scripts/biweekly_offline_render.py periods --land-id 19994 --plan plan.json

    # 2. 生成全部已结束分期的报告
    python scripts/biweekly_offline_render.py render --land-id 19994 \\
        --plan plan.json --products products/ [--scenes scenes.json] [--weather weather.json] \\
        --out out/

    # 3. 已有接口侧 facts（内部接口 /inputs 的返回）时，直接渲染这一期
    python scripts/biweekly_offline_render.py render-facts --facts facts.json --products products/ --out out/

本脚本只读输入文件、只写 --out 目录：不连接数据库、Redis、对象存储和内部接口，不下载影像和
气象数据，不调用 Celery 任务，不回写任何接口。报告由正式入口 generate_official_biweekly_pdf
生成；输入缺什么，报告就少什么并写明原因，不补造数据，也不会把真实地块标成示例数据。
输入文件的字段说明见 docs/design/biweekly-offline-render.md。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "services" / "ingest", ROOT / "packages" / "agric_satellite_analysis_common"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

# 离线出报用不到这些连接配置；从本进程环境中移除，保证即使在下载机的终端里运行也连不到任何服务。
SERVICE_ENV = (
    "DATABASE_URL",
    "DATABASE_URL_SYNC",
    "REDIS_URL",
    "CELERY_BROKER_URL",
    "CELERY_RESULT_BACKEND",
    "API_BASE_URL",
    "INTERNAL_API_TOKEN",
    "MYSQL_SOURCE_URL",
    "CLOUDAMQP_URL",
    "OSS_ACCESS_KEY_ID",
    "OSS_ACCESS_KEY_SECRET",
)


def isolate_environment() -> list[str]:
    removed = [name for name in SERVICE_ENV if os.environ.pop(name, None) is not None]
    return removed


def code_version() -> dict[str, str]:
    """The code this run used: the packaged version file, else the git commit."""
    stamp = ROOT / "CODE_VERSION.json"
    if stamp.is_file():
        try:
            data = json.loads(stamp.read_text(encoding="utf-8"))
            return {"source": "CODE_VERSION.json", "commit": str(data.get("commit") or "unknown")}
        except (OSError, json.JSONDecodeError):
            pass
    try:
        commit = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        return {"source": "git", "commit": commit + ("+modified" if dirty else "")}
    except (OSError, subprocess.SubprocessError):
        return {"source": "unknown", "commit": "unknown"}


def _input_entry(path: str | Path | None) -> dict[str, str] | None:
    from app.reports.biweekly.offline_render import sha256_file

    if not path:
        return None
    source = Path(path)
    if source.is_dir():
        return {"path": str(source), "kind": "directory"}
    return {"path": str(source), "sha256": sha256_file(source)}


def _load_calendar(args: argparse.Namespace) -> dict:
    from app.reports.biweekly.offline_render import (
        OfflineInputError,
        calendar_from_plan,
        load_json,
        load_rows,
    )

    if args.plan:
        if args.plan_batches or args.plan_tasks:
            raise OfflineInputError("--plan 与 --plan-batches/--plan-tasks 只能用一种")
        payload = load_json(args.plan)
    elif args.plan_batches and args.plan_tasks:
        payload = {
            "batches": load_rows(args.plan_batches),
            "tasks": load_rows(args.plan_tasks),
            "mapping_rows": load_rows(args.plan_mapping) if args.plan_mapping else None,
        }
    else:
        raise OfflineInputError("需要 --plan，或同时给出 --plan-batches 与 --plan-tasks")
    calendar = calendar_from_plan(args.land_id, payload)
    if not calendar.get("seasons"):
        note = calendar.get("note") or "方案中没有本田的小麦种植季"
        raise OfflineInputError(f"{note}（线上同样不会为没有方案的地块列出种植季和出报）")
    return calendar


def _add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--land-id", required=True, help="地块编号，例如 19994")
    parser.add_argument("--plan", help="方案文件（JSON）：Smart 原始行，或接口 seasons / periods 的返回")
    parser.add_argument("--plan-batches", help="Smart 批次行（JSON 或 CSV），与 --plan-tasks 配合")
    parser.add_argument("--plan-tasks", help="Smart 种植任务行（JSON 或 CSV）")
    parser.add_argument("--plan-mapping", help="crop_remote_phase_mapping 行（可选；不给则用代码内试点映射）")


def cmd_periods(args: argparse.Namespace) -> int:
    from app.reports.biweekly.offline_render import export_windows

    calendar = _load_calendar(args)
    print(json.dumps({"land_id": str(args.land_id), "seasons": export_windows(calendar)}, ensure_ascii=False, indent=2))
    return 0


def _land_name(args: argparse.Namespace, products: list[dict]) -> tuple[str | None, str]:
    if args.land_name:
        return args.land_name.strip(), "argument"
    names = {str(item.get("land_name") or "").strip() for item in products}
    names.discard("")
    names.discard(str(args.land_id))
    if len(names) == 1:
        return names.pop(), "product_json"
    return None, "none"


def cmd_render(args: argparse.Namespace) -> int:
    from app.reports.biweekly.offline_render import (
        check_scene_rows,
        load_products,
        load_rows,
        load_weather,
        render_land,
        scene_rows_from_products,
    )

    calendar = _load_calendar(args)
    products, notes = load_products(args.products, args.land_id)
    if args.scenes:
        scene_rows = check_scene_rows(load_rows(args.scenes), args.land_id)
        source = "parcel_scene_products"
    else:
        # 没有景清单导出时，用场景产品 JSON 的顶层字段（与入库到 parcel_scene_products 的列相同）。
        scene_rows = check_scene_rows(scene_rows_from_products(products), args.land_id)
        source = "parcel_product_json"
    weather = load_weather(args.weather, args.land_id) if args.weather else None
    land_name, name_source = _land_name(args, products)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = render_land(
        land_id=str(args.land_id),
        calendar=calendar,
        scene_rows=scene_rows,
        observation_source=source,
        products=products,
        out_dir=out,
        weather=weather,
        land_name=land_name,
        seasons=args.season,
        period_ids=args.period,
        as_of=args.as_of,
        include_current=args.include_current,
    )
    report.update(
        {
            "code": code_version(),
            "renderer": "generate_official_biweekly_pdf(offline=True)",
            "observation_source": source,
            "land_name": land_name,
            "land_name_source": name_source,
            "plan_input": calendar.get("plan_input"),
            "weather_provided": weather is not None,
            "inputs": {
                "plan": _input_entry(args.plan),
                "plan_batches": _input_entry(args.plan_batches),
                "plan_tasks": _input_entry(args.plan_tasks),
                "plan_mapping": _input_entry(args.plan_mapping),
                "scenes": _input_entry(args.scenes),
                "weather": _input_entry(args.weather),
                "products": {"path": str(args.products), "files_used": len(products)},
            },
            "input_notes": notes,
        }
    )
    manifest = out / "manifest.json"
    manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    failed = 0
    for season in report["seasons"]:
        print(f"种植季 {season['season_id']}（{season['start']} 至 {season['end_inclusive']}）")
        for row in season["periods"]:
            label = f"  第{row['period_id']}期 {row['start']}—{row['end_inclusive']}"
            status = row["status"]
            if status == "ready":
                print(f"{label}  正式 PDF：{row['dir']}/{row['artifact']}")
            elif status == "evidence_limited":
                print(f"{label}  无可用光学观测，证据受限简报：{row['dir']}/{row['artifact']}")
            elif status == "failed":
                failed += 1
                print(f"{label}  失败：{row.get('error')}")
            else:
                print(f"{label}  {row.get('note')}")
    for note in notes:
        print(f"提示：{note}")
    print(f"清单：{manifest}")
    return 2 if failed else 0


def cmd_render_facts(args: argparse.Namespace) -> int:
    from app.reports.biweekly.offline_render import (
        facts_from_export,
        load_json,
        load_products,
        products_for_period,
        render_facts,
    )

    facts, status = facts_from_export(load_json(args.facts))
    products, notes = (load_products(args.products, str(facts["land_id"])) if args.products else ([], []))
    out = Path(args.out)
    result = render_facts(facts, status, out, products=products_for_period(products, facts))
    result.update(
        {
            "land_id": str(facts["land_id"]),
            "code": code_version(),
            "renderer": "generate_official_biweekly_pdf(offline=True)",
            "inputs": {"facts": _input_entry(args.facts), "products": _input_entry(args.products)},
            "input_notes": notes,
        }
    )
    manifest = out / "manifest.json"
    manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"{result['status']}：{out / result['artifact']}")
    print(f"清单：{manifest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)

    periods = commands.add_parser("periods", help="列出种植季、分期和各类数据应导出的日期范围")
    _add_plan_arguments(periods)
    periods.set_defaults(run=cmd_periods)

    render = commands.add_parser("render", help="按方案逐期生成报告")
    _add_plan_arguments(render)
    render.add_argument("--products", required=True, help="地块场景产品 JSON 目录（{日期}_S2.json 等）")
    render.add_argument("--scenes", help="parcel_scene_products 导出行（JSON 或 CSV）；不给则取产品 JSON 顶层字段")
    render.add_argument("--weather", help="weather_daily 导出行（JSON 或 CSV）；不给则报告不含气象章节")
    render.add_argument("--land-name", help="地块名称；不给则取产品 JSON 中一致的 land_name")
    render.add_argument("--season", action="append", help="只生成指定种植季，可重复；默认全部")
    render.add_argument("--period", action="append", help="只生成指定期号，可重复；默认全部")
    render.add_argument("--as-of", help="按哪一天判断分期是否已结束（YYYY-MM-DD，默认今天，北京时间）")
    render.add_argument("--include-current", action="store_true", help="同时生成尚未结束的当前分期")
    render.add_argument("--out", required=True, help="输出目录")
    render.set_defaults(run=cmd_render)

    facts = commands.add_parser("render-facts", help="用已导出的接口侧 facts 渲染单期报告")
    facts.add_argument("--facts", required=True, help="内部接口 /inputs 的返回，或其中的 facts 对象（JSON）")
    facts.add_argument("--products", help="地块场景产品 JSON 目录；不给则只用田块均值，不出田内分布图")
    facts.add_argument("--out", required=True, help="输出目录")
    facts.set_defaults(run=cmd_render_facts)

    args = parser.parse_args(argv)
    removed = isolate_environment()
    if removed:
        print("已从本进程环境中移除服务连接配置：" + "、".join(removed), file=sys.stderr)

    from agric_satellite_analysis_common.biweekly.models import ReportError
    from app.reports.biweekly.offline_render import OfflineInputError

    try:
        return args.run(args)
    except OfflineInputError as exc:
        print(f"输入有误：{exc}", file=sys.stderr)
        return 1
    except ReportError as exc:
        print(f"无法出报：{exc.code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
