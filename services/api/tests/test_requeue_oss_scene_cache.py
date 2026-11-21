"""Helpers for re-enqueueing already-uploaded OSS scene JSONs into results/cache."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


def _load_script():
    path = Path(__file__).resolve().parents[3] / "scripts" / "requeue_oss_scene_cache.py"
    spec = importlib.util.spec_from_file_location("requeue_oss_scene_cache", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RequeueOssSceneCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_script()

    def test_parse_and_build_envelope(self) -> None:
        key = "s1s2_parcel/json/19994/2026-03-15_S2.json"
        info = self.mod.parse_scene_oss_key(key)
        self.assertEqual(
            info,
            {
                "json_oss_key": key,
                "land_id": "19994",
                "date": "2026-03-15",
                "sensor": "S2",
            },
        )
        envelope = self.mod.build_cache_result(
            land_id="19994",
            date="2026-03-15",
            sensor="S2",
            json_url="https://oss.example/19994/2026-03-15_S2.json",
            json_oss_key=key,
        )
        self.assertEqual(envelope["status"], "success")
        self.assertEqual(
            envelope["oss_urls"],
            {"2026-03-15_S2": "https://oss.example/19994/2026-03-15_S2.json"},
        )
        self.assertEqual(envelope["extras"]["kind"], "parcel_scene_product")
        self.assertEqual(envelope["extras"]["land_id"], "19994")
        self.assertEqual(envelope["extras"]["json_oss_key"], key)
        self.assertIn("oss_urls", envelope)
        self.assertTrue(envelope["oss_urls"])

    def test_list_and_envelopes_use_storage_public_url(self) -> None:
        keys = [
            "s1s2_parcel/json/19994/2026-03-15_S2.json",
            "s1s2_parcel/json/19994/readme.txt",
            "s1s2_parcel/json/19994/2026-03-20_S1.json",
        ]
        storage = SimpleNamespace(
            list_keys=lambda prefix, suffix="": [
                key for key in keys if key.startswith(prefix) and key.endswith(suffix)
            ],
            public_url=lambda key: f"https://oss.example/{key}",
        )
        found = self.mod.list_land_scene_keys(storage, "19994")
        self.assertEqual(
            found,
            [
                "s1s2_parcel/json/19994/2026-03-15_S2.json",
                "s1s2_parcel/json/19994/2026-03-20_S1.json",
            ],
        )
        envelopes = self.mod.envelopes_for_keys(storage, found)
        self.assertEqual(len(envelopes), 2)
        self.assertEqual(
            envelopes[1]["oss_urls"]["2026-03-20_S1"],
            "https://oss.example/s1s2_parcel/json/19994/2026-03-20_S1.json",
        )
