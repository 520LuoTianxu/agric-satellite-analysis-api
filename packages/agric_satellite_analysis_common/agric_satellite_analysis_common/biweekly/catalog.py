"""Local 6417 18-issue pack catalog. No production database access."""

from __future__ import annotations

import json
from pathlib import Path

from .calendar import iter_periods, planting_season_label
from .models import ReportError

DEFAULT_PACK = Path("/workspace/reports/field6417-2025-2026")
DEFAULT_V8 = Path("/workspace/reports/field6417-20260404-20260417-v8")
LAND_ID = "6417"
SEASON_START = "2025-11-01"
SEASON_END_INCLUSIVE = "2026-06-30"
ALLOWED_REFERENCE_IDS = {"15933"}


class LocalPackCatalog:
    def __init__(self, pack_root: Path | None = None, v8_root: Path | None = None):
        self.pack_root = Path(pack_root or DEFAULT_PACK)
        self.v8_root = Path(v8_root or DEFAULT_V8)
        if not (self.pack_root / "manifest.json").is_file():
            raise ReportError("CATALOG_UNAVAILABLE")
        self.manifest = json.loads((self.pack_root / "manifest.json").read_text())
        if str(self.manifest.get("land_id")) != LAND_ID:
            raise ReportError("CATALOG_UNAVAILABLE")

    def season(self) -> dict:
        label = planting_season_label(SEASON_START, SEASON_END_INCLUSIVE)
        return {
            "season_id": label,
            "label": f"{label} 种植季",
            "land_id": LAND_ID,
            "crop": "winter_wheat",
            "crop_name": "冬小麦",
            "start": SEASON_START,
            "end_inclusive": SEASON_END_INCLUSIVE,
            "end_exclusive": "2026-07-01",
            "cadence_days": 14,
            "note": "按6417研究包：从2025-11-01起每14个自然日一期，半开区间。仅含本地冬小麦夹具，不是本田已关联方案。",
        }

    def list_seasons(self, land_id: str) -> list[dict]:
        if str(land_id) != LAND_ID:
            raise ReportError("LAND_NOT_IN_LOCAL_CATALOG")
        return [self.season()]

    def periods(self, land_id: str, season_id: str) -> list[dict]:
        season = self.season()
        if str(land_id) != LAND_ID:
            raise ReportError("LAND_NOT_IN_LOCAL_CATALOG")
        if season_id != season["season_id"]:
            raise ReportError("SEASON_NOT_FOUND")
        built = iter_periods(season["start"], season["end_inclusive"])
        by_issue = {int(item["issue"]): item for item in self.manifest["issues"]}
        if len(built) != len(by_issue):
            raise ReportError("CATALOG_PERIOD_MISMATCH")
        rows = []
        for item in built:
            src = by_issue[item["issue"]]
            if src["start"] != item["start"] or src["end"] != item["end_inclusive"]:
                raise ReportError("CATALOG_PERIOD_MISMATCH")
            optical = int(src.get("optical_descriptive_days") or 0)
            rows.append(
                {
                    **item,
                    "snapshot": src["snapshot"],
                    "pack_pdf": src["pdf"],
                    "optical_descriptive_days": optical,
                    "s1_products": int(src.get("s1_products") or 0),
                    "weather_days": int(src.get("weather_days") or 0),
                    "precipitation_mm": src.get("precipitation_mm"),
                    "has_eligible_optics": optical > 0,
                    "plan_status": src.get("plan_status"),
                }
            )
        return rows

    def period(self, land_id: str, season_id: str, period_id: str) -> dict:
        for row in self.periods(land_id, season_id):
            if row["period_id"] == str(period_id):
                return row
        raise ReportError("PERIOD_NOT_FOUND")

    def load_snapshot(self, relative: str) -> dict:
        path = self.pack_root / relative
        if not path.is_file() or not path.resolve().is_relative_to(self.pack_root.resolve()):
            raise ReportError("SNAPSHOT_NOT_FOUND")
        return json.loads(path.read_text())

    def v8_optical_metrics(self) -> dict | None:
        path = self.v8_root / "sources" / "optical_metrics.json"
        if path.is_file():
            return json.loads(path.read_text())
        return None

    def v8_pdf(self) -> Path | None:
        matches = list(self.v8_root.glob("6417_*_v8.pdf"))
        return matches[0] if matches else None

    def v8_prior_period(self) -> dict | None:
        path = self.v8_root / "sources" / "prior_period.json"
        if path.is_file():
            return json.loads(path.read_text())
        return None
