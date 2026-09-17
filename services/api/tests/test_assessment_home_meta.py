"""首页报告选择应排除失败任务、已删除地块和缺少 PDF 的任务。"""

from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.middleware.auth import ANON_USER, OrgContext
from app.routers.assessment import get_latest_available_assessment_meta


class AssessmentHomeMetaTests(IsolatedAsyncioTestCase):
    async def test_returns_report_and_only_queries_available_pdfs(self):
        job = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        ctx = OrgContext(user=ANON_USER, org_id=None, role="owner")

        self.assertIs(await get_latest_available_assessment_meta(ctx, db), job)
        # 校验实际 PostgreSQL 查询，防止首页被失败任务或软删除地块遮蔽。
        query = db.execute.call_args.args[0]
        sql = str(
            query.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        self.assertIn("land_parcels.deleted_at IS NULL", sql)
        self.assertIn("jobs.status = 'succeeded'", sql)
        self.assertIn("jobs.type = 'assessment_report'", sql)
        self.assertIn("->> 'object_key'", sql)
        self.assertIn("IS NOT NULL", sql)
        self.assertIn("!= ''", sql)
        self.assertIn("jobs.finished_at DESC NULLS LAST", sql)
        self.assertIn("LIMIT 1", sql)

    async def test_no_report_returns_404(self):
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        ctx = OrgContext(user=ANON_USER, org_id=None, role="owner")

        with self.assertRaises(HTTPException) as raised:
            await get_latest_available_assessment_meta(ctx, db)
        self.assertEqual(raised.exception.status_code, 404)
