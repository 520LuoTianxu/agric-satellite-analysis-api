"""Stdlib tests for decloud flag, trigger, quality scoring, and official gate."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.decloud import (
    DECLOUD_SOURCE,
    DecloudQualityInputs,
    decloud_backend,
    decloud_cloud_min_pct,
    decloud_enabled,
    decloud_oss_sensor,
    decloud_scene_id,
    score_decloud,
    should_trigger_decloud,
)
from app.core.agri_classify import (
    is_decloud_product,
    is_official_optical_product,
    official_s2_sql,
)


class DecloudFlagTests(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DECLOUD_ENABLED", None)
            self.assertFalse(decloud_enabled())

    def test_explicit_on(self) -> None:
        with patch.dict(os.environ, {"DECLOUD_ENABLED": "1"}):
            self.assertTrue(decloud_enabled())

    def test_cloud_min_aligns_with_drought_30(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DECLOUD_CLOUD_MIN_PCT", None)
            self.assertEqual(decloud_cloud_min_pct(), 30.0)

    def test_backend_dummy(self) -> None:
        with patch.dict(os.environ, {"DECLOUD_BACKEND": "dummy"}):
            self.assertEqual(decloud_backend(), "dummy")

    def test_product_identity_is_additive(self) -> None:
        self.assertEqual(
            decloud_scene_id("2024-07-15"), "stac_bridge_2024-07-15_S2_decloud"
        )
        self.assertEqual(decloud_oss_sensor(), "S2_decloud")
        self.assertNotEqual(decloud_oss_sensor(), "S2")


class TriggerTests(unittest.TestCase):
    def test_over_30_flag_triggers(self) -> None:
        self.assertTrue(should_trigger_decloud(cloud_cover_over_30=True))

    def test_parcel_cloud_over_30_triggers(self) -> None:
        self.assertTrue(should_trigger_decloud(parcel_cloud_cover_pct=42.0))

    def test_clear_parcel_does_not_trigger(self) -> None:
        self.assertFalse(
            should_trigger_decloud(
                cloud_cover_over_30=False,
                parcel_cloud_cover_pct=12.0,
                cloud_cover=8.0,
            )
        )

    def test_stac_cloud_over_threshold(self) -> None:
        self.assertTrue(should_trigger_decloud(cloud_cover=55.0))


class QualityScoreTests(unittest.TestCase):
    def test_healthy_reconstruction_is_good(self) -> None:
        result = score_decloud(
            DecloudQualityInputs(
                rgb_mean=0.18,
                rgb_mean_raw=0.42,
                rgb_std=0.08,
                rgb_std_raw=0.09,
                ndvi_mean=0.62,
                neighbor_ndvi_mean=0.65,
            )
        )
        self.assertEqual(result.quality, "good")
        self.assertTrue(result.is_official)
        self.assertGreaterEqual(result.score, 0.7)
        self.assertEqual(result.reasons, [])

    def test_rgb_still_bright_is_bad(self) -> None:
        result = score_decloud(
            DecloudQualityInputs(
                rgb_mean=0.52,
                rgb_mean_raw=0.55,
                rgb_std=0.07,
                rgb_std_raw=0.08,
                ndvi_mean=0.4,
                neighbor_ndvi_mean=0.45,
            )
        )
        self.assertEqual(result.quality, "bad")
        self.assertFalse(result.is_official)
        self.assertIn("rgb_still_bright", result.reasons)

    def test_tiny_rgb_delta_is_not_good(self) -> None:
        result = score_decloud(
            DecloudQualityInputs(
                rgb_mean=0.40,
                rgb_mean_raw=0.41,
                rgb_std=0.07,
                rgb_std_raw=0.08,
                ndvi_mean=0.35,
                neighbor_ndvi_mean=0.40,
            )
        )
        self.assertIn(result.quality, ("fair", "bad"))
        self.assertFalse(result.is_official)
        self.assertIn("tiny_rgb_delta", result.reasons)

    def test_spatial_std_collapse_is_bad(self) -> None:
        result = score_decloud(
            DecloudQualityInputs(
                rgb_mean=0.20,
                rgb_mean_raw=0.40,
                rgb_std=0.008,
                rgb_std_raw=0.09,
                ndvi_mean=0.5,
                neighbor_ndvi_mean=0.55,
            )
        )
        self.assertEqual(result.quality, "bad")
        self.assertIn("spatial_std_collapse", result.reasons)

    def test_ndvi_far_below_neighbors_is_bad(self) -> None:
        result = score_decloud(
            DecloudQualityInputs(
                rgb_mean=0.18,
                rgb_mean_raw=0.40,
                rgb_std=0.07,
                rgb_std_raw=0.08,
                ndvi_mean=0.12,
                neighbor_ndvi_mean=0.60,
            )
        )
        self.assertEqual(result.quality, "bad")
        self.assertIn("ndvi_far_below_neighbors", result.reasons)

    def test_missing_neighbors_does_not_penalize(self) -> None:
        result = score_decloud(
            DecloudQualityInputs(
                rgb_mean=0.18,
                rgb_mean_raw=0.40,
                rgb_std=0.08,
                rgb_std_raw=0.09,
                ndvi_mean=0.55,
                neighbor_ndvi_mean=None,
            )
        )
        self.assertEqual(result.quality, "good")


class OfficialGateTests(unittest.TestCase):
    def test_raw_clear_is_official(self) -> None:
        self.assertTrue(
            is_official_optical_product(
                source="stac_direct",
                scene_id="stac_bridge_2024-07-01_S2",
                cloud_cover_over_30=False,
                parcel_cloud_cover_pct=12.0,
            )
        )

    def test_raw_cloudy_is_not_official(self) -> None:
        self.assertFalse(
            is_official_optical_product(
                source="stac_direct",
                scene_id="stac_bridge_2024-07-01_S2",
                cloud_cover_over_30=True,
                parcel_cloud_cover_pct=None,
            )
        )

    def test_good_decloud_is_official(self) -> None:
        self.assertTrue(
            is_official_optical_product(
                source=DECLOUD_SOURCE,
                scene_id="stac_bridge_2024-07-01_S2_decloud",
                decloud_quality="good",
                cloud_cover_over_30=False,
            )
        )

    def test_fair_and_bad_decloud_are_audit_only(self) -> None:
        for q in ("fair", "bad"):
            self.assertFalse(
                is_official_optical_product(
                    source=DECLOUD_SOURCE,
                    scene_id="stac_bridge_2024-07-01_S2_decloud",
                    decloud_quality=q,
                    cloud_cover_over_30=False,
                    parcel_cloud_cover_pct=0.0,
                ),
                msg=q,
            )

    def test_scene_id_suffix_detects_decloud(self) -> None:
        self.assertTrue(is_decloud_product(None, "stac_bridge_2024-07-01_S2_decloud"))
        self.assertFalse(is_decloud_product("stac_direct", "stac_bridge_2024-07-01_S2"))

    def test_sql_predicate_requires_good(self) -> None:
        sql = official_s2_sql("s")
        self.assertIn("uncrtaints_decloud", sql)
        self.assertIn("decloud_quality", sql)
        self.assertIn("'good'", sql)
        self.assertIn("_decloud", sql)


class UncrtaintsCheckpointTests(unittest.TestCase):
    """Loader tests with fake weights. CI ingest has no torch/numpy extras."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._added_modules = _stub_optional_modules()
        from app.core import uncrtaints as uncrtaints_mod

        cls.u = uncrtaints_mod

    def test_strip_netg_prefix_drops_wrapper_keys(self) -> None:
        stripped = self.u._strip_netg_prefix(
            {
                "netG.in_conv.weight": 1,
                "netG.out_block.0.weight": 2,
                "module.netG.in_block.0.weight": 3,
                "criterion.foo": 9,
            }
        )
        self.assertEqual(
            stripped,
            {
                "in_conv.weight": 1,
                "out_block.0.weight": 2,
                "in_block.0.weight": 3,
            },
        )

    def test_rename_in_out_blocks_digit_minus_one(self) -> None:
        renamed = self.u._rename_in_out_blocks(
            {"in_block1.conv.weight": 1, "out_block1.proj.bias": 2, "in_conv.weight": 3}
        )
        self.assertEqual(
            renamed,
            {
                "in_block.0.conv.weight": 1,
                "out_block.0.proj.bias": 2,
                "in_conv.weight": 3,
            },
        )

    def test_resolve_prefers_pth_tar_under_experiment_dir(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exp = root / "diagonal_1"
            exp.mkdir()
            (exp / "other.pth").write_bytes(b"pth")
            target = exp / "model.pth.tar"
            target.write_bytes(b"tar")
            with patch.dict(
                os.environ,
                {
                    "UNCRTAINTS_CHECKPOINT_DIR": str(root),
                    "UNCRTAINTS_CHECKPOINT_NAME": "diagonal_1",
                },
            ):
                self.assertEqual(self.u._resolve_checkpoint_file(), target)
            with patch.dict(
                os.environ,
                {
                    "UNCRTAINTS_CHECKPOINT_DIR": str(exp),
                    "UNCRTAINTS_CHECKPOINT_NAME": "diagonal_1",
                },
            ):
                self.assertEqual(self.u._resolve_checkpoint_file(), target)
            with patch.dict(
                os.environ,
                {"UNCRTAINTS_CHECKPOINT_DIR": str(target)},
            ):
                self.assertEqual(self.u._resolve_checkpoint_file(), target)

    def test_load_strips_netg_and_reads_conf(self) -> None:
        import json
        import sys
        import tempfile
        import types
        from pathlib import Path

        class FakeGenerator:
            last_kwargs: dict = {}
            last_state: dict = {}
            load_calls: list = []

            def __init__(self, **kwargs):
                type(self).last_kwargs = kwargs
                self.expected = {"in_conv.weight", "out_block.0.weight"}

            def load_state_dict(self, state, strict=False):
                type(self).load_calls.append(dict(state))
                type(self).last_state = dict(state)
                missing = [k for k in self.expected if k not in state]
                unexpected = [k for k in state if k not in self.expected]
                return missing, unexpected

            def to(self, device):
                return self

            def eval(self):
                return self

        FakeGenerator.load_calls = []

        class FakeTorch:
            @staticmethod
            def load(path, map_location=None):
                del path, map_location
                return {
                    "epoch": 12,
                    "state_dict": {
                        "netG.in_conv.weight": 1,
                        "netG.out_block.0.weight": 2,
                        "criterion.loss.weight": 9,
                    },
                }

        src_mod = types.ModuleType("src")
        bb_mod = types.ModuleType("src.backbones")
        u_mod = types.ModuleType("src.backbones.uncrtaints")
        u_mod.UNCRTAINTS = FakeGenerator
        extra = {
            "torch": FakeTorch,
            "src": src_mod,
            "src.backbones": bb_mod,
            "src.backbones.uncrtaints": u_mod,
        }
        with tempfile.TemporaryDirectory() as tmp:
            exp = Path(tmp) / "diagonal_1"
            exp.mkdir()
            (exp / "model.pth.tar").write_bytes(b"fake")
            (exp / "conf.json").write_text(
                json.dumps(
                    {
                        "encoder_widths": "[128]",
                        "decoder_widths": "[128,128,128,128,128]",
                        "out_conv": "[13]",
                        "mean_nonLinearity": True,
                        "var_nonLinearity": "softplus",
                        "agg_mode": "att_group",
                        "encoder_norm": "group",
                        "decoder_norm": "batch",
                        "n_head": 16,
                        "d_model": 256,
                        "d_k": 4,
                        "pad_value": 0,
                        "padding_mode": "reflect",
                        "positional_encoding": True,
                        "covmode": "diag",
                        "scale_by": 10.0,
                        "separate_out": False,
                        "use_v": False,
                        "block_type": "mbconv",
                        "pretrain": False,
                    }
                )
            )
            env = {
                "UNCRTAINTS_CHECKPOINT_DIR": str(exp),
                "UNCRTAINTS_CHECKPOINT_NAME": "diagonal_1",
                "UNCRTAINTS_HOME": "",
                "DECLOUD_USE_SAR": "1",
                "DECLOUD_INPUT_T": "3",
            }
            with patch.dict(sys.modules, extra), patch.dict(os.environ, env):
                infer = self.u.UncrtainTSInferencer(device="cpu")

        self.assertIsInstance(infer.model, FakeGenerator)
        self.assertEqual(FakeGenerator.last_kwargs["encoder_widths"], [128])
        self.assertEqual(FakeGenerator.last_kwargs["out_conv"], [26])
        self.assertEqual(FakeGenerator.last_kwargs["block_type"], "mbconv")
        self.assertFalse(FakeGenerator.last_kwargs["is_mono"])
        self.assertEqual(
            FakeGenerator.last_state,
            {"in_conv.weight": 1, "out_block.0.weight": 2},
        )
        self.assertTrue(
            all(not k.startswith("netG.") for k in FakeGenerator.last_state)
        )
        self.assertEqual(len(FakeGenerator.load_calls), 1)

    def test_in_block_rename_fallback_loads_cleanly(self) -> None:
        class FakeGenerator:
            def __init__(self):
                self.expected = {"in_block.0.conv.weight"}

            def load_state_dict(self, state, strict=False):
                missing = [k for k in self.expected if k not in state]
                unexpected = [k for k in state if k not in self.expected]
                return missing, unexpected

        warnings: list[tuple] = []

        def _warn(*args, **kwargs):
            warnings.append((args, kwargs))

        with patch.object(self.u.logger, "warning", _warn):
            missing, unexpected = self.u._load_state_into_generator(
                FakeGenerator(),
                {"netG.in_block1.conv.weight": 1},
                Path("model.pth.tar"),
            )
        self.assertEqual(missing, [])
        self.assertEqual(unexpected, [])
        self.assertEqual(warnings, [])


def _stub_optional_modules() -> list[str]:
    """CI ingest job does not install numpy/structlog; stub only if missing."""
    import sys
    import types

    added: list[str] = []
    if "numpy" not in sys.modules:
        numpy_mod = types.ModuleType("numpy")
        numpy_mod.ndarray = type("ndarray", (), {})  # type: ignore[attr-defined]
        numpy_mod.float32 = float  # type: ignore[attr-defined]
        sys.modules["numpy"] = numpy_mod
        added.append("numpy")
    if "structlog" not in sys.modules:
        structlog_mod = types.ModuleType("structlog")

        class _Log:
            def warning(self, *args, **kwargs):
                return None

            def info(self, *args, **kwargs):
                return None

        structlog_mod.get_logger = lambda: _Log()  # type: ignore[attr-defined]
        sys.modules["structlog"] = structlog_mod
        added.append("structlog")
    return added


if __name__ == "__main__":
    unittest.main()
