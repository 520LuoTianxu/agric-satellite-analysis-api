"""Unit tests for WRITE_INDEX_COGS / UPLOAD_SCENE_JSON defaults."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class WriteIndexCogsTests(unittest.TestCase):
    def test_agri_defaults_off(self) -> None:
        from app.core.index_cogs import write_index_cogs_enabled

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WRITE_INDEX_COGS", None)
            self.assertFalse(write_index_cogs_enabled(is_agri=True))

    def test_classic_defaults_on(self) -> None:
        from app.core.index_cogs import write_index_cogs_enabled

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WRITE_INDEX_COGS", None)
            self.assertTrue(write_index_cogs_enabled(is_agri=False))

    def test_explicit_off_overrides_classic(self) -> None:
        from app.core.index_cogs import write_index_cogs_enabled

        with patch.dict(os.environ, {"WRITE_INDEX_COGS": "0"}):
            self.assertFalse(write_index_cogs_enabled(is_agri=False))
            self.assertFalse(write_index_cogs_enabled(is_agri=True))

    def test_explicit_on_is_agri_opt_in(self) -> None:
        from app.core.index_cogs import write_index_cogs_enabled

        with patch.dict(os.environ, {"WRITE_INDEX_COGS": "1"}):
            self.assertTrue(write_index_cogs_enabled(is_agri=True))
            self.assertTrue(write_index_cogs_enabled(is_agri=False))

    def test_empty_string_uses_default(self) -> None:
        from app.core.index_cogs import write_index_cogs_enabled

        with patch.dict(os.environ, {"WRITE_INDEX_COGS": "  "}):
            self.assertFalse(write_index_cogs_enabled(is_agri=True))
            self.assertTrue(write_index_cogs_enabled(is_agri=False))


class UploadSceneJsonTests(unittest.TestCase):
    def test_defaults_on(self) -> None:
        from app.core.index_cogs import upload_scene_json_enabled

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UPLOAD_SCENE_JSON", None)
            self.assertTrue(upload_scene_json_enabled())

    def test_explicit_off(self) -> None:
        from app.core.index_cogs import upload_scene_json_enabled

        with patch.dict(os.environ, {"UPLOAD_SCENE_JSON": "false"}):
            self.assertFalse(upload_scene_json_enabled())


if __name__ == "__main__":
    unittest.main()
