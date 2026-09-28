"""UnCRtainTS窗口缓存版本隔离测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.decloud_cache import cache_root


class DecloudCacheVersionTests(unittest.TestCase):
    def test_s1_calibration_change_uses_new_cache_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"DECLOUD_CACHE_DIR": tmp}
        ):
            self.assertEqual(cache_root(), Path(tmp) / "v4_sigma0_lut")


if __name__ == "__main__":
    unittest.main()
