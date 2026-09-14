"""field resolve prefers internal HTTP when API_BASE_URL is set."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_fake_common = SimpleNamespace()
sys.modules.setdefault("openfarm_common", _fake_common)
sys.modules.setdefault(
    "openfarm_common.celery_app",
    SimpleNamespace(celery_client=MagicMock()),
)
sys.modules.setdefault(
    "openfarm_common.database_sync",
    SimpleNamespace(SyncSession=MagicMock()),
)
sys.modules.setdefault(
    "openfarm_common.mq_results",
    SimpleNamespace(publish_task_result=MagicMock()),
)
sys.modules.setdefault(
    "openfarm_common.mq_schemas",
    SimpleNamespace(TaskMessage=object),
)
sys.modules.setdefault("sqlalchemy", SimpleNamespace(text=MagicMock()))

_ia = types.ModuleType("openfarm_common.internal_api")
_ia.internal_api_enabled = lambda: True
_ia.resolve_field = MagicMock(
    return_value={"field_id": "http-field", "land_id": "http-land"}
)
sys.modules["openfarm_common.internal_api"] = _ia

from app import handler as handler_mod


class FieldResolveHttpTests(unittest.TestCase):
    def test_uses_http_when_enabled(self) -> None:
        with (
            patch.object(_ia, "internal_api_enabled", return_value=True),
            patch.object(
                _ia,
                "resolve_field",
                return_value={"field_id": "http-field", "land_id": "http-land"},
            ) as rf,
            patch.dict("sys.modules", {"openfarm_common.internal_api": _ia}),
        ):
            fid, lid = handler_mod._resolve_field_and_land(None, "P1", None)
        self.assertEqual(fid, "http-field")
        self.assertEqual(lid, "http-land")
        rf.assert_called()

    def test_falls_back_db_when_disabled(self) -> None:
        session = MagicMock()
        session.execute.return_value.first.return_value = ("db-field",)
        with (
            patch.object(_ia, "internal_api_enabled", return_value=False),
            patch.object(handler_mod, "SyncSession", return_value=session),
        ):
            fid, lid = handler_mod._resolve_field_and_land(None, None, "LAND1")
        self.assertEqual(fid, "db-field")
        self.assertEqual(lid, "LAND1")
        session.close.assert_called()


if __name__ == "__main__":
    unittest.main()
