"""10km 规划：线程池执行不阻塞事件循环，几何缓存优化结果与原实现一致。"""

from __future__ import annotations

import asyncio
import random
import time
import unittest
from datetime import date
from types import SimpleNamespace

from shapely.geometry import box, mapping
from shapely.strtree import STRtree

from app.services import virtual_area_planner as vap
from app.services.satellite_batch import (
    build_satellite_batch_jobs,
    build_satellite_batch_jobs_async,
    find_invalid_satellite_lands,
)


def _lands(n: int, seed: int = 7):
    r = random.Random(seed)
    out = []
    for i in range(n):
        cx, cy = 115 + r.random() * 0.6, 38 + r.random() * 0.4
        w, h = r.uniform(0.001, 0.006), r.uniform(0.001, 0.006)
        out.append(SimpleNamespace(
            land_id=f"L{i:05d}",
            boundary_geojson=mapping(box(cx, cy, cx + w, cy + h)),
            boundary_srid=4326,
        ))
    return out


class PlannerCacheEquivalenceTests(unittest.TestCase):
    def test_scarcity_scores_identical_with_and_without_cache(self):
        parcels = vap.normalize_parcels(_lands(400))
        geoms = [p.geometry for p in parcels]
        tree = STRtree(geoms)
        ident = {id(g): i for i, g in enumerate(geoms)}
        active = {p.land_id: p for p in parcels[::2]}
        cache = vap._GeometryCache(parcels, vap.DEFAULT_WINDOW_SIDE_M)
        with_cache = vap._scarcity_scores(
            parcels, side_m=vap.DEFAULT_WINDOW_SIDE_M, active_parcels=active,
            tree=tree, identity_index=ident, cache=cache,
        )
        # 对照：逐个调用 geometry.bounds/centroid 的原始算法。
        expected = {}
        for land_id, parcel in active.items():
            o = parcel.geometry.centroid
            lb = vap._approximate_metric_bounds(parcel.geometry, origin_x=o.x, origin_y=o.y)
            rect = vap._feasible_center_rect_from_bounds(lb, vap.DEFAULT_WINDOW_SIDE_M / 2)
            if rect is None:
                expected[land_id] = 2.0
                continue
            eligible = 0
            for idx in tree.query(vap._rough_query_box(parcel.geometry, vap.DEFAULT_WINDOW_SIDE_M)):
                other = parcels[int(idx)]
                if other.land_id == land_id or other.land_id not in active:
                    continue
                ob = vap._approximate_metric_bounds(other.geometry, origin_x=o.x, origin_y=o.y)
                if vap._rect_intersects(rect, vap._feasible_center_rect_from_bounds(ob, vap.DEFAULT_WINDOW_SIDE_M / 2)):
                    eligible += 1
            size = min(max(lb[2] - lb[0], lb[3] - lb[1]) / vap.DEFAULT_WINDOW_SIDE_M, 1.0)
            expected[land_id] = 1.0 / (eligible + 1.0) + 0.25 * size
        self.assertEqual(with_cache, expected)

    def test_plan_is_deterministic_and_covers_all_lands(self):
        lands = _lands(300)
        a = vap.plan_virtual_areas(lands)
        b = vap.plan_virtual_areas(list(reversed(lands)))
        sig = lambda plans: sorted((p.land_ids, p.aggregation_bbox) for p in plans)
        self.assertEqual(sig(a), sig(b))
        members = sorted(x for p in a for x in p.land_ids)
        self.assertEqual(members, sorted(l.land_id for l in lands))


class PlannerOffloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_build_matches_sync_and_keeps_loop_responsive(self):
        lands = _lands(600)
        kwargs = dict(date_from=date(2026, 9, 1), date_to=date(2026, 9, 10), sensors=("S2",))
        lags: list[float] = []
        stop = asyncio.Event()

        async def probe():
            while not stop.is_set():
                t = time.perf_counter()
                await asyncio.sleep(0.05)
                lags.append(time.perf_counter() - t - 0.05)

        task = asyncio.create_task(probe())
        groups, jobs = await build_satellite_batch_jobs_async(lands, **kwargs)
        stop.set()
        await task
        sync_groups, sync_jobs = build_satellite_batch_jobs(lands, **kwargs)
        self.assertEqual(
            [(g.anchor_land_id, g.land_ids, g.download_bbox) for g in groups],
            [(g.anchor_land_id, g.land_ids, g.download_bbox) for g in sync_groups],
        )
        self.assertEqual(len(jobs), len(sync_jobs))
        self.assertLess(max(lags), 0.5)

    async def test_find_invalid_lands_in_thread(self):
        lands = _lands(5)
        lands[2].boundary_srid = 3857
        lands[4].boundary_geojson = None
        self.assertEqual(await find_invalid_satellite_lands(lands), {"L00002", "L00004"})
