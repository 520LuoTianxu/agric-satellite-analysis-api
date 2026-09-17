"""用隔离的内存数据库验证个人已读和租户范围，不连接业务数据库。"""

import unittest
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Annotated
from unittest.mock import AsyncMock

import httpx
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.middleware.alert_auth import AlertContext, get_alert_context
from app.models.tables import Alert, AlertRead
from app.routers.alerts import (
    _page,
    _read_insert,
    _scoped_alerts,
    alerts_summary,
    mark_read,
    update_alert,
)
from app.schemas.monitoring import AlertUpdate


class PersonalReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        self.connection = self.engine.connect()
        for sql in (
            "ATTACH DATABASE ':memory:' AS agric_satellite",
            "CREATE TABLE agric_satellite.farms (id TEXT, name TEXT, deleted_at TEXT)",
            "CREATE TABLE agric_satellite.land_parcels "
            "(land_id TEXT PRIMARY KEY, base_id TEXT, land_name TEXT, farm_id TEXT, deleted_at TEXT)",
            "CREATE TABLE agric_satellite.alerts (id TEXT PRIMARY KEY, land_id TEXT, date DATE, "
            "severity TEXT, rule_name TEXT, rule_params_json TEXT, message TEXT, status TEXT, "
            "index_type TEXT, weather_context TEXT, soil_context TEXT, created_at DATETIME, updated_at DATETIME)",
            "CREATE TABLE agric_satellite.alert_reads (base_id TEXT, user_id TEXT, alert_id TEXT, "
            "read_at DATETIME DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(base_id,user_id,alert_id))",
            "INSERT INTO agric_satellite.land_parcels VALUES "
            "('A','38','甲地块',NULL,NULL),('B','39','乙地块',NULL,NULL),"
            "('C','38','删除地块',NULL,'2026-09-01'),('D',NULL,'无归属',NULL,NULL)",
        ):
            self.connection.exec_driver_sql(sql)
        self.session = Session(bind=self.connection)
        self.db = SimpleNamespace(
            execute=AsyncMock(side_effect=self.session.execute),
            flush=AsyncMock(side_effect=self.session.flush),
        )
        self.a = AlertContext(base_id="38", user_id="209")
        self.b = AlertContext(base_id="38", user_id="210")
        self.other = AlertContext(base_id="39", user_id="209")
        self.first = self.add_alert("A")
        self.foreign = self.add_alert("B")
        self.add_alert("C")
        self.add_alert("D")
        self.session.flush()

    def tearDown(self):
        self.session.close()
        self.connection.close()
        self.engine.dispose()

    def add_alert(self, land_id, status="open"):
        alert = Alert(
            id=uuid.uuid4(),
            land_id=land_id,
            date=date(2026, 9, 17),
            severity="high",
            rule_name="ndvi_threshold",
            message="长势偏低",
            status=status,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.session.add(alert)
        return alert

    async def test_read_is_personal_and_idempotent(self):
        before = await alerts_summary(self.a, self.db)
        self.assertEqual((before.open_total, before.unread_total), (1, 1))
        first = await mark_read(self.first.id, self.a, self.db)
        again = await mark_read(self.first.id, self.a, self.db)
        self.assertTrue(first.is_read)
        self.assertEqual(first.read_at, again.read_at)
        self.assertEqual((await alerts_summary(self.a, self.db)).unread_total, 0)
        self.assertEqual((await alerts_summary(self.b, self.db)).unread_total, 1)
        self.assertEqual((await alerts_summary(self.other, self.db)).unread_total, 1)

    async def test_bulk_read_covers_all_pages_and_closed_without_changing_status(self):
        for _ in range(234):
            self.add_alert("A")
        self.add_alert("A", status="closed")
        self.session.flush()
        page = await _page(self.db, _scoped_alerts(self.a), 10, 0)
        self.assertEqual((len(page.items), page.total), (10, 236))
        marked = self.session.execute(_read_insert(self.a)).all()
        self.assertEqual(len(marked), 236)
        self.assertEqual(self.session.execute(_read_insert(self.a)).all(), [])
        after = await alerts_summary(self.a, self.db)
        self.assertEqual((after.open_total, after.unread_total), (235, 0))
        self.assertEqual((await alerts_summary(self.b, self.db)).unread_total, 236)
        self.assertEqual((await alerts_summary(self.other, self.db)).unread_total, 1)
        # 批量操作之后新增的预警仍然未读。
        self.add_alert("A")
        self.session.flush()
        self.assertEqual((await alerts_summary(self.a, self.db)).unread_total, 1)

    async def test_foreign_alert_cannot_be_read_or_closed(self):
        for action in (
            mark_read(self.foreign.id, self.a, self.db),
            update_alert(
                self.foreign.id, AlertUpdate(status="closed"), self.a, self.db
            ),
        ):
            with self.assertRaises(HTTPException) as error:
                await action
            self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(self.foreign.status, "open")
        self.assertEqual(
            self.session.scalar(select(func.count()).select_from(AlertRead)), 0
        )

    async def test_closing_does_not_mark_anyone_read_and_filters_work(self):
        result = await update_alert(
            self.first.id, AlertUpdate(status="closed"), self.a, self.db
        )
        self.assertFalse(result.is_read)
        await mark_read(self.first.id, self.a, self.db)
        unread = _scoped_alerts(self.b).where(AlertRead.read_at.is_(None))
        page = await _page(self.db, unread, 10, 0)
        self.assertEqual(page.total, 1)
        self.assertFalse(page.items[0].is_read)
        self.assertEqual(page.items[0].land_name, "甲地块")
        mine = await _page(self.db, _scoped_alerts(self.a), 10, 0)
        self.assertTrue(mine.items[0].is_read)


class AlertContextTests(unittest.IsolatedAsyncioTestCase):
    async def request_context(self, headers):
        app = FastAPI()

        @app.get("/context")
        async def context(ctx: Annotated[AlertContext, Depends(get_alert_context)]):
            return ctx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/context", headers=headers)

    async def test_headers_work_without_token_or_with_invalid_token(self):
        # 预警上下文无需农业服务；Authorization 缺失、格式错误、失效均不影响它。
        for authorization in (None, "invalid-token", "Bearer expired-token"):
            headers = {"hr-base-id": "37", "x-account-id": "209"}
            if authorization is not None:
                headers["Authorization"] = authorization
            response = await self.request_context(headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"base_id": "37", "user_id": "209"})

    async def test_missing_or_invalid_identifiers_are_rejected(self):
        for header, max_length in (("hr-base-id", 64), ("x-account-id", 128)):
            for value in (
                None,
                "",
                " ",
                "0",
                "-1",
                "1.5",
                "abc",
                "1,2",
                "1" * (max_length + 1),
            ):
                with self.subTest(header=header, value=value):
                    headers = {"hr-base-id": "37", "x-account-id": "209"}
                    if value is None:
                        headers.pop(header)
                    else:
                        headers[header] = value
                    response = await self.request_context(headers)
                    self.assertEqual(response.status_code, 400)

    async def test_ids_are_normalized_and_non_ascii_ids_rejected(self):
        self.assertEqual(
            await get_alert_context(" 037 ", " 0209 "),
            AlertContext(base_id="37", user_id="209"),
        )
        with self.assertRaises(HTTPException) as error:
            await get_alert_context("37", "２０９")
        self.assertEqual(error.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
