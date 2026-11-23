"""Safe diagnostics for biweekly scene-query failures, and DATE bind typing."""

from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
from agric_satellite_analysis_common.biweekly.models import ReportError
from sqlalchemy.sql import text
from sqlalchemy.sql.elements import BindParameter
from sqlalchemy.sql.sqltypes import NullType
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg

from app.services.biweekly_report_flow import _enrich_payload
from app.services.biweekly_scenes import (
    SCENE_SQL,
    _as_bind_date,
    _eligible_optical,
    describe_query_error,
    load_scene_observations,
    safe_error_text,
)


JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbb"
DSN = "postgresql+asyncpg://openfarm:s3cret-pass@db-host/openfarm"
STATEMENT = (
    "SELECT land_id, date, sensor, scene_id, ndvi_avg FROM "
    "agric_satellite.parcel_scene_products WHERE land_id = $1 "
    "AND date >= $2 AND date < $3"
)
PARAMS = {
    "land_id": "32696",
    "start": "2025-09-06",
    "end_exclusive": "2025-09-20",
    "password": "s3cret-pass",
    "authorization": f"Bearer {JWT}",
}


class FakeOrig(Exception):
    def __init__(
        self, message: str, *, sqlstate: str | None = None, pgcode: str | None = None
    ):
        super().__init__(message)
        if sqlstate is not None:
            self.sqlstate = sqlstate
        if pgcode is not None:
            self.pgcode = pgcode


class FakeDBAPIError(Exception):
    def __init__(self, orig: BaseException, statement: str, params: dict):
        self.orig = orig
        self.statement = statement
        self.params = params
        super().__init__(
            f"(asyncpg.exceptions.DataError) {orig} [SQL: {statement}] "
            f"[parameters: {params}] {DSN} Bearer {JWT}"
        )
        self.__cause__ = orig


class FakeLock(Exception):
    sqlstate = "55P03"


class FakeUndefinedColumn(Exception):
    sqlstate = "42703"


def _data_error_for_str_date() -> asyncpg.DataError:
    # Mirrors asyncpg/protocol/prepared_stmt.pyx when date_encode calls
    # str.toordinal() after PostgreSQL infers a DATE parameter.
    raw = "'2025-09-06'"
    inner = AttributeError("'str' object has no attribute 'toordinal'")
    return asyncpg.DataError(f"invalid input for query argument $2: {raw} ({inner})")


class DescribeQueryErrorTests(unittest.TestCase):
    def test_asyncpg_str_date_bind_is_type_mismatch_without_sqlstate(self) -> None:
        orig = _data_error_for_str_date()
        wrapped = FakeDBAPIError(orig, STATEMENT, PARAMS)
        info = describe_query_error(wrapped)
        self.assertEqual(info["err_type"], "FakeDBAPIError")
        self.assertEqual(info["driver_exc_type"], "DataError")
        self.assertEqual(info["sqlstate"], "22000")
        self.assertEqual(info["err_kind"], "type_mismatch")
        self.assertIn("DataError", info["exc_chain"] or "")
        self.assertIn("FakeDBAPIError", info["exc_chain"] or "")
        blob = " ".join(str(v) for v in info.values())
        self.assertNotIn("s3cret-pass", blob)
        self.assertNotIn(JWT, blob)
        self.assertNotIn("Bearer eyJ", blob)
        self.assertNotIn(DSN, blob)
        self.assertNotIn("postgresql+asyncpg://", blob)
        # Long/sensitive bind dict must not be echoed.
        self.assertNotIn("end_exclusive", blob)
        self.assertNotIn("[parameters:", blob)

    def test_undefined_column_uses_sqlstate_not_generic_dbapierror(self) -> None:
        orig = FakeUndefinedColumn('column "parcel_cloud_cover_pct" does not exist')
        wrapped = FakeDBAPIError(orig, STATEMENT, PARAMS)
        info = describe_query_error(wrapped)
        self.assertEqual(info["sqlstate"], "42703")
        self.assertEqual(info["err_kind"], "undefined_column")
        self.assertEqual(info["driver_exc_type"], "FakeUndefinedColumn")
        self.assertIn("parcel_cloud_cover_pct", info["safe_orig"] or "")

    def test_lock_timeout_sqlstate_is_not_connection_closed(self) -> None:
        orig = FakeLock("could not obtain lock")
        orig.__class__ = type("LockNotAvailableError", (FakeLock,), {})
        info = describe_query_error(orig)
        self.assertEqual(info["sqlstate"], "55P03")
        self.assertEqual(info["err_kind"], "lock_timeout")
        self.assertNotEqual(info["err_kind"], "connection_closed")

    def test_interface_error_connection_closed_has_no_sqlstate(self) -> None:
        class InterfaceError(Exception):
            pass

        exc = InterfaceError("the underlying connection is closed")
        info = describe_query_error(exc)
        self.assertIsNone(info["sqlstate"])
        self.assertEqual(info["err_kind"], "connection_closed")
        self.assertEqual(info["driver_exc_type"], "InterfaceError")

    def test_type_mismatch_sqlstate_distinct_from_undefined_column(self) -> None:
        orig = FakeOrig("datatype mismatch", sqlstate="42804")
        info = describe_query_error(orig)
        self.assertEqual(info["err_kind"], "type_mismatch")
        self.assertEqual(info["sqlstate"], "42804")

    def test_bare_exception_without_sqlstate_or_orig_is_other(self) -> None:
        info = describe_query_error(RuntimeError("boom"))
        self.assertEqual(info["err_type"], "RuntimeError")
        self.assertEqual(info["driver_exc_type"], "RuntimeError")
        self.assertIsNone(info["sqlstate"])
        self.assertEqual(info["err_kind"], "other")
        self.assertEqual(info["safe_orig"], "boom")
        self.assertEqual(info["exc_chain"], "RuntimeError")

    def test_safe_error_text_redacts_uri_jwt_and_sql_parameter_blocks(self) -> None:
        raw = (
            f"failed {DSN} Authorization: Bearer {JWT} password=visible-secret "
            f"[SQL: {STATEMENT}] [parameters: {PARAMS}]"
        )
        cleaned = safe_error_text(raw)
        self.assertNotIn("s3cret-pass", cleaned)
        self.assertNotIn("visible-secret", cleaned)
        self.assertNotIn(JWT, cleaned)
        self.assertNotIn("Bearer eyJ", cleaned)
        self.assertIn("[uri:redacted]", cleaned)
        self.assertIn("[SQL:redacted]", cleaned)
        # The [SQL: ...] block already includes trailing [parameters: ...].
        self.assertNotIn("visible-secret", cleaned)
        self.assertNotIn("[parameters: {", cleaned)
        self.assertLessEqual(len(cleaned), 240)

    def test_long_parameter_payload_is_truncated_and_not_echoed(self) -> None:
        huge = "x" * 5000
        cleaned = safe_error_text(f"note [parameters: {{'blob': '{huge}'}}]")
        self.assertNotIn(huge, cleaned)
        self.assertLessEqual(len(cleaned), 240)


class AsyncpgDateBindTests(unittest.TestCase):
    def test_text_scene_sql_binds_are_untyped_nulltype(self) -> None:
        compiled = text(SCENE_SQL).compile(dialect=PGDialect_asyncpg())
        for name in ("start", "end_exclusive"):
            bind = compiled.binds[name]
            self.assertIsInstance(bind, BindParameter)
            self.assertIsInstance(bind.type, NullType)

    def test_asyncpg_date_encode_rejects_str_the_same_way_as_a_date_bind(self) -> None:
        # date_encode (asyncpg/pgproto/codecs/datetime.pyx) calls obj.toordinal()
        # with no str coercion. prepared_stmt.pyx wraps that as DataError.
        with self.assertRaises(AttributeError) as raised:
            "2025-09-06".toordinal()  # type: ignore[attr-defined]
        self.assertIn("toordinal", str(raised.exception))
        self.assertEqual(date(2025, 9, 6).toordinal(), date(2025, 9, 6).toordinal())
        err = _data_error_for_str_date()
        self.assertIsInstance(err, asyncpg.DataError)
        self.assertIn("invalid input for query argument $2", str(err))
        self.assertIn("2025-09-06", str(err))
        self.assertIn("toordinal", str(err))
        # This can coexist with DMS success: a string *literal* in SQL is not a bind.

    def test_as_bind_date_accepts_iso_str_date_and_datetime(self) -> None:
        self.assertEqual(_as_bind_date("2025-09-06"), date(2025, 9, 6))
        self.assertEqual(_as_bind_date(date(2025, 9, 20)), date(2025, 9, 20))
        self.assertEqual(
            _as_bind_date(datetime(2025, 9, 6, 15, 30, 0)), date(2025, 9, 6)
        )
        self.assertEqual(_as_bind_date("2025-09-06T00:00:00+08:00"), date(2025, 9, 6))
        # Legal empty window still produces valid DATE values.
        start = _as_bind_date("2025-09-06")
        end = _as_bind_date("2025-09-06")
        self.assertEqual(start, end)

    def test_as_bind_date_rejects_empty_and_invalid(self) -> None:
        with self.assertRaises(ValueError):
            _as_bind_date("")
        with self.assertRaises(ValueError):
            _as_bind_date("   ")
        with self.assertRaises(ValueError):
            _as_bind_date("not-a-date")
        with self.assertRaises(ValueError):
            _as_bind_date("2025-13-40")


def _session(execute):
    session = MagicMock()
    session.execute = execute

    class _CM:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    return _CM()


class LoadSceneObservationsTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_rows_and_binds_datetime_date(self) -> None:
        captured: dict = {}

        async def execute(stmt, params=None):
            captured["params"] = params
            row = {
                "land_id": "32696",
                "date": date(2025, 9, 10),
                "sensor": "S2",
                "scene_id": "S2A",
                "ndvi_avg": 0.2,
                "evi_avg": 0.1,
                "ndre_avg": None,
                "ndmi_avg": 0.05,
                "cloud_cover": 5.0,
                "parcel_cloud_cover_pct": 0.0,
                "vv_avg": None,
                "vh_avg": None,
            }
            result = MagicMock()
            result.mappings.return_value.all.return_value = [row]
            return result

        with (
            patch("app.services.biweekly_scenes.settings") as settings,
            patch("app.core.database.async_session", return_value=_session(execute)),
            patch("app.services.biweekly_scenes.logger.warning") as warning,
        ):
            settings.database_url = "postgresql+asyncpg://x@localhost/db"
            rows = await load_scene_observations(
                "32696", "2025-09-06", "2025-09-20", job_id="bj-22dbe8eff79b205b"
            )
        self.assertIsNotNone(rows)
        assert rows is not None
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quality_status"], "eligible_descriptive")
        self.assertEqual(captured["params"]["start"], date(2025, 9, 6))
        self.assertEqual(captured["params"]["end_exclusive"], date(2025, 9, 20))
        self.assertIsInstance(captured["params"]["start"], date)
        self.assertNotIsInstance(captured["params"]["start"], datetime)
        self.assertEqual(captured["params"]["land_id"], "32696")
        warning.assert_not_called()

    async def test_query_failure_logs_safe_fields_and_returns_none(self) -> None:
        orig = _data_error_for_str_date()
        wrapped = FakeDBAPIError(orig, STATEMENT, PARAMS)

        async def execute(stmt, params=None):
            raise wrapped

        with (
            patch("app.services.biweekly_scenes.settings") as settings,
            patch("app.core.database.async_session", return_value=_session(execute)),
            patch("app.services.biweekly_scenes.logger.warning") as warning,
        ):
            settings.database_url = (
                "postgresql+asyncpg://openfarm:s3cret-pass@localhost/db"
            )
            rows = await load_scene_observations(
                "32696",
                "2025-09-06",
                "2025-09-20",
                job_id="bj-22dbe8eff79b205b",
            )
        self.assertIsNone(rows)
        warning.assert_called_once()
        args, kwargs = warning.call_args
        self.assertEqual(args[0], "biweekly_scene_query_failed")
        self.assertEqual(kwargs["land_id"], "32696")
        self.assertEqual(kwargs["start"], "2025-09-06")
        self.assertEqual(kwargs["end_exclusive"], "2025-09-20")
        self.assertEqual(kwargs["job_id"], "bj-22dbe8eff79b205b")
        self.assertEqual(kwargs["err_kind"], "type_mismatch")
        self.assertEqual(kwargs["driver_exc_type"], "DataError")
        self.assertEqual(kwargs["sqlstate"], "22000")
        self.assertNotIn("exc_info", kwargs)
        blob = " ".join(f"{k}={v}" for k, v in kwargs.items())
        self.assertNotIn("s3cret-pass", blob)
        self.assertNotIn(JWT, blob)
        self.assertNotIn("Authorization", blob)

    async def test_invalid_window_does_not_hit_db_and_stays_none(self) -> None:
        execute = AsyncMock(side_effect=AssertionError("db should not be called"))
        with (
            patch("app.services.biweekly_scenes.settings") as settings,
            patch("app.core.database.async_session", return_value=_session(execute)),
            patch("app.services.biweekly_scenes.logger.warning") as warning,
        ):
            settings.database_url = "postgresql+asyncpg://x@localhost/db"
            rows = await load_scene_observations("32696", "not-a-date", "2025-09-20")
        self.assertIsNone(rows)
        execute.assert_not_called()
        warning.assert_called_once()
        kwargs = warning.call_args.kwargs
        self.assertEqual(kwargs["err_type"], "ValueError")
        self.assertEqual(kwargs["err_kind"], "other")

    async def test_enrich_payload_still_raises_scene_query_failed(self) -> None:
        with patch(
            "app.services.biweekly_report_flow.load_scene_observations",
            new=AsyncMock(return_value=None),
        ):
            period = {
                "period_id": "1",
                "start": "2025-09-06",
                "end_inclusive": "2025-09-19",
                "end_exclusive": "2025-09-20",
            }
            with self.assertRaises(ReportError) as raised:
                await _enrich_payload(
                    "32696", period, [period], job_id="bj-22dbe8eff79b205b"
                )
        self.assertEqual(raised.exception.code, "SCENE_QUERY_FAILED")


class UserDmsRowsEligibilityTests(unittest.TestCase):
    """Existing eligibility only — do not change decloud quality rules here."""

    def test_clear_s2_metrics_on_sep_10_and_13_are_eligible(self) -> None:
        for day in (date(2025, 9, 10), date(2025, 9, 13)):
            row = {
                "sensor": "S2",
                "date": day,
                "parcel_cloud_cover_pct": 0,
                "cloud_cover": 0,
                "ndvi_avg": 0.2,
                "evi_avg": 0.1,
                "ndre_avg": None,
                "ndmi_avg": 0.05,
            }
            self.assertTrue(_eligible_optical(row), msg=str(day))

    def test_decloud_cloud_100_still_excluded_by_existing_rule(self) -> None:
        row = {
            "sensor": "S2",
            "date": date(2025, 9, 8),
            "parcel_cloud_cover_pct": 100,
            "cloud_cover": 100,
            "ndvi_avg": 0.2,
            "evi_avg": 0.1,
            "ndre_avg": None,
            "ndmi_avg": 0.05,
        }
        self.assertFalse(_eligible_optical(row))
