"""生成可重复的模拟数据简报和页面夹具；不读取真实地块、不连接数据库。"""

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import fitz

from app.reports.parcel_insights import render_report
from app.schemas.parcel_insights import InsightsRequest
from app.services.parcel_insights import build_insights

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tmp" / "parcel-insights-qa"


def fixture_points(year, scale=1):
    return [
        {
            "date": (date(year, 3, 1) + timedelta(days=i * 10)).isoformat(),
            "ndvi": round(value * scale, 3),
            "ndvi_avg": round(value * scale, 3),
            "ndmi": round(value * 0.3, 3),
            "evi": round(value * 0.7, 3),
            "official": True,
            "source": "stac_direct",
            "scene_id": f"fixture-{year}-{i}",
        }
        for i, value in enumerate(
            [0.18, 0.2, 0.4, 0.52, 0.65, 0.74, 0.78, 0.72, 0.21, 0.19, 0.18, 0.18]
        )
    ]


async def create_snapshot():
    lands = [
        SimpleNamespace(
            land_id=key,
            land_name=f"示例基地 · {name}（模拟数据）",
            crop_type="玉米",
            group_name="模拟项目",
            land_area_mu=120 + i * 30,
            area_ha=8 + i * 2,
            boundary_geojson={
                "type": "Polygon",
                "coordinates": [
                    [
                        [116, 39],
                        [116.004, 39],
                        [116.004, 39.004],
                        [116, 39.004],
                        [116, 39],
                    ]
                ],
            },
        )
        for i, (key, name) in enumerate([("A", "东区一号地"), ("B", "东区二号地")])
    ]
    series = {
        land.land_id: fixture_points(2024, 0.92 - i * 0.12)
        + fixture_points(2025, 1 - i * 0.17)
        for i, land in enumerate(lands)
    }
    db = AsyncMock()
    land_result, rain_result = MagicMock(), MagicMock()
    land_result.scalars.return_value.all.return_value = lands
    rain_result.all.return_value = [
        (land.land_id, date(2025, 3, 1) + timedelta(days=i), 2.5 if i % 5 == 0 else 0)
        for land in lands
        for i in range(115)
    ]
    pixel_results = []
    for offset in range(2):
        pixels = [
            {
                "lon": 116 + x * 0.0001,
                "lat": 39 + y * 0.0001,
                "NDVI": 0.24
                if x > 12 and y > 8
                else 0.52 - offset * 0.1 + (x % 3) * 0.02,
                "clear": 1,
            }
            for x in range(20)
            for y in range(16)
        ]
        result = MagicMock()
        result.scalar_one_or_none.return_value = {
            "format": "lonlat_v1",
            "pixels": pixels,
        }
        pixel_results.append(result)
    db.execute.side_effect = [land_result, rain_result, *pixel_results]
    request = InsightsRequest(
        land_ids=["A", "B"],
        start_date="2025-03-01",
        end_date="2025-06-30",
        reference_year=2024,
        title="地块横向对比与农服复盘 · 模拟数据",
        brand_name="乡合农服 · 演示报告",
        events=[
            {
                "land_id": "A",
                "date": "2025-04-10",
                "action": "灌溉记录（模拟）",
                "note": "用于验证记录留存和前后变化，非真实服务效果。",
                "control_land_id": "B",
                "window_days": 40,
            }
        ],
    )
    with patch(
        "app.services.parcel_insights.load_points", AsyncMock(return_value=series)
    ):
        snapshot = await build_insights(db, request)
    snapshot.update(
        snapshot_id="11111111-1111-4111-8111-111111111111",
        created_at="2026-09-21T08:00:00+00:00",
    )
    return snapshot


async def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    snapshot = await create_snapshot()
    (OUTPUT / "snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    pdf = render_report(snapshot)
    (OUTPUT / "parcel-insights-demo.pdf").write_bytes(pdf)
    doc = fitz.open(stream=pdf, filetype="pdf")
    for i, page in enumerate(doc):
        page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).save(OUTPUT / f"page-{i + 1}.png")
    content = "".join(page.get_text() for page in doc)
    for required in [
        "模拟数据",
        "地块对比概览",
        "历史同期",
        "农服措施前后复盘",
        "2025-03-01",
        "2025-06-30",
    ]:
        assert required in content, required
    assert snapshot["comparison"]["count"] >= 3
    assert snapshot["events"][0]["relative_change"] is not None
    print(
        json.dumps(
            {
                "pages": len(doc),
                "pdf": str(OUTPUT / "parcel-insights-demo.pdf"),
                "snapshot": str(OUTPUT / "snapshot.json"),
                "progress": snapshot["progress_counts"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
