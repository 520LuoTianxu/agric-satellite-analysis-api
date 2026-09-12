"""Joint stretch keeps relative band ratios better than independent stretch."""

from __future__ import annotations

import io
import unittest

try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:  # CI ingest job is dependency-light
    HAS_NUMPY = False


@unittest.skipUnless(HAS_NUMPY, "numpy required")
class JointStretchTests(unittest.TestCase):
    def test_joint_preserves_green_dominance(self) -> None:
        from app.core.true_color_preview import joint_stretch_rgb

        h, w = 32, 32
        r = np.full((h, w), 0.05, dtype=np.float64)
        g = np.full((h, w), 0.18, dtype=np.float64)
        b = np.full((h, w), 0.04, dtype=np.float64)
        mask = np.ones((h, w), dtype=bool)

        jr, jg, jb = joint_stretch_rgb(r, g, b, valid_mask=mask)
        self.assertGreater(float(jg.mean()), float(jr.mean()))
        self.assertGreater(float(jg.mean()), float(jb.mean()))

    def test_independent_stretch_washes_ratios(self) -> None:
        """Document why we left independent percentile stretch: equalizes bands."""
        h, w = 32, 32
        rng = np.random.default_rng(0)
        r = 0.05 + 0.002 * rng.standard_normal((h, w))
        g = 0.18 + 0.002 * rng.standard_normal((h, w))
        b = 0.04 + 0.002 * rng.standard_normal((h, w))
        mask = np.ones((h, w), dtype=bool)

        def _indep(band: np.ndarray) -> np.ndarray:
            out = np.zeros(band.shape, dtype=np.uint8)
            vals = band[mask]
            lo, hi = np.percentile(vals, [2, 98])
            if hi <= lo:
                return out
            scaled = np.clip((band - lo) / (hi - lo), 0, 1)
            out[mask] = (scaled[mask] * 255).astype(np.uint8)
            return out

        ir, ig, ib = _indep(r), _indep(g), _indep(b)
        self.assertAlmostEqual(float(ir.mean()), float(ig.mean()), delta=8.0)
        self.assertAlmostEqual(float(ig.mean()), float(ib.mean()), delta=8.0)

        from app.core.true_color_preview import joint_stretch_rgb

        jr, jg, jb = joint_stretch_rgb(r, g, b, valid_mask=mask)
        joint_spread = float(jg.mean()) - float(jr.mean())
        indep_spread = abs(float(ig.mean()) - float(ir.mean()))
        self.assertGreater(joint_spread, indep_spread)

    def test_dn_scale_10000(self) -> None:
        from app.core.true_color_preview import joint_stretch_rgb

        r = np.full((8, 8), 500.0)
        g = np.full((8, 8), 1800.0)
        b = np.full((8, 8), 400.0)
        mask = np.ones((8, 8), dtype=bool)
        jr, jg, jb = joint_stretch_rgb(r, g, b, valid_mask=mask)
        self.assertGreater(float(jg.mean()), float(jr.mean()))

    def test_visual_uint8_passthrough(self) -> None:
        from app.core.true_color_preview import render_scene_rgb_jpeg

        rgb = np.zeros((16, 16, 3), dtype=np.uint8)
        rgb[..., 1] = 200
        rgb[..., 0] = 40
        rgb[..., 2] = 30
        jpg = render_scene_rgb_jpeg(visual=rgb)
        self.assertIsNotNone(jpg)
        self.assertGreater(len(jpg), 50)
        self.assertEqual(jpg[:2], b"\xff\xd8")

    def test_field_png_has_alpha(self) -> None:
        from app.core.true_color_preview import render_field_rgb_png
        from PIL import Image

        bands = {
            "B04": np.full((10, 10), 0.05),
            "B03": np.full((10, 10), 0.18),
            "B02": np.full((10, 10), 0.04),
        }
        mask = np.zeros((10, 10), dtype=bool)
        mask[2:8, 2:8] = True
        png = render_field_rgb_png(bands, mask)
        self.assertIsNotNone(png)
        img = Image.open(io.BytesIO(png))
        self.assertEqual(img.mode, "RGBA")
        arr = np.asarray(img)
        self.assertTrue(np.all(arr[~mask, 3] == 0))
        self.assertTrue(np.all(arr[mask, 3] == 255))



    def test_outline_is_one_pixel_ring(self) -> None:
        from app.core.true_color_preview import outline_mask_from_filled

        mask = np.zeros((12, 12), dtype=bool)
        mask[3:9, 3:9] = True
        outline = outline_mask_from_filled(mask)
        self.assertTrue(np.any(outline))
        self.assertTrue(np.all(outline <= mask))
        # Interior of the square should be cleared.
        self.assertFalse(bool(outline[5, 5]))
        self.assertTrue(bool(outline[3, 5]))
        self.assertTrue(bool(outline[8, 5]))
        # Outline should be much thinner than filled.
        self.assertLess(int(outline.sum()), int(mask.sum()) // 2)

    def test_draw_red_outline_paints_bright_red(self) -> None:
        from app.core.true_color_preview import draw_red_outline, outline_mask_from_filled
        from app.core.true_color_preview import render_scene_rgb_jpeg
        from PIL import Image

        rgb = np.zeros((20, 20, 3), dtype=np.uint8)
        rgb[..., 1] = 180
        mask = np.zeros((20, 20), dtype=bool)
        mask[5:15, 5:15] = True
        outline = outline_mask_from_filled(mask)
        painted = draw_red_outline(rgb, outline)
        self.assertEqual(tuple(painted[5, 10]), (220, 30, 30))
        self.assertEqual(tuple(painted[10, 10]), (0, 180, 0))

        jpg = render_scene_rgb_jpeg(visual=rgb, field_outline=outline)
        self.assertIsNotNone(jpg)
        arr = np.asarray(Image.open(io.BytesIO(jpg)).convert("RGB"))
        # JPEG is lossy — red channel should dominate on outline pixels.
        ys, xs = np.where(outline)
        sample = arr[ys[0], xs[0]]
        self.assertGreater(int(sample[0]), int(sample[1]))
        self.assertGreater(int(sample[0]), int(sample[2]))


if __name__ == "__main__":
    unittest.main()
