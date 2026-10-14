"""回归：用原型保存的 2878 数据立方体重跑 proj_ts_m1_v1，须复现 897.7 亩 / 15.0% / 未种植 368.6 亩。

数据在下载机分析目录（不入库）；不存在时跳过。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest
from agric_satellite_analysis_common import project_harvest as ph

from app.core import project_harvest as core

BASE = Path(os.getenv("PROJECT_HARVEST_REGRESSION_DIR", "/workspace/analysis/group2878"))
pytestmark = pytest.mark.skipif(not (BASE / "run_N" / "m1_full.npz").exists(), reason="no 2878 regression data")


def run_regression() -> dict:
    from pyproj import Transformer
    from shapely.geometry import shape
    from shapely.ops import transform as stf

    params = ph.merge_params(None)
    lands = json.loads((BASE / "lands_2878.json").read_text())
    dd = ph.dedup_parcels(lands, params)
    kept = {r["land_id"]: r for r in dd["rows"] if r["kept"]}
    aoi = {p["land_id"]: p["aoi"] for p in json.loads((BASE / "parcels.json").read_text())}
    to = Transformer.from_crs(4326, 32650, always_xy=True).transform
    stats = []
    for a in "NS":
        F = np.load(BASE / f"run_{a}" / "m1_full.npz", allow_pickle=True)
        C = np.load(BASE / f"run_{a}" / "cube.npz")
        CL = F["CL"]
        dates = [str(x) for x in F["dates"]]
        x0, y1 = float(F["x0"]), float(F["y1"])
        T, H, W = CL.shape
        NDVI, score, thr, _ = core.m1_score(C["B4"].copy(), C["B8"].copy(), C["B11"].copy(), CL)
        assert np.allclose(np.nan_to_num(score), np.nan_to_num(F["score"])), "score mismatch"
        ws = {}
        FR = np.zeros((H, W), np.float32)
        for lid, r in kept.items():
            if aoi.get(lid) != a:
                continue
            res = core.parcel_weights(stf(to, shape(r["clipped_geojson"])), x0, y1, H, W)
            if res is None:
                continue
            ws[lid] = res
            r0, c0, w = res
            FR[r0 : r0 + w.shape[0], c0 : c0 + w.shape[1]] += w
        FR = np.clip(FR, 0, 1)
        out = core.classify(dates, NDVI, CL, score, FR, params, "2026-09-10")
        for lid, (r0, c0, w) in ws.items():
            by = {}
            for t, d in enumerate(dates):
                by[d] = ph.parcel_date_row(core.tier_sums(out["tiers"][t], r0, c0, w), float(w.sum()), kept[lid]["clipped_area_mu"])
            pick = ph.choose_pick(by, params["pick_cloud_max"])
            for d, row in by.items():
                stats.append({"land_id": lid, "obs_date": d, "block_key": a, "is_pick": d == pick, **row})
    meta = {lid: r for lid, r in kept.items()}
    summ = ph.build_summaries(stats, meta, "pick")
    tot = next(s for s in summ if s["scope_type"] == "pick" and s["scope_key"] == "all:all")
    return tot


def test_2878_regression():
    tot = run_regression()
    harvested = tot["high_mu"] + tot["medium_mu"]
    assert tot["parcel_count"] == 286
    assert abs(harvested - 897.7) < 2.0
    assert abs(tot["harvested_pct"] - 15.0) <= 0.1
    assert abs(tot["noncrop_mu"] - 368.6) < 2.0


if __name__ == "__main__":
    t = run_regression()
    print(json.dumps({k: (round(v, 2) if isinstance(v, float) else v) for k, v in t.items()}, ensure_ascii=False))
