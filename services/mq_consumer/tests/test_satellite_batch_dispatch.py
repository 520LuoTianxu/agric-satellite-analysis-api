"""MQ与HTTP claim都必须派发分组任务，不展开成单地块回填。"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agric_satellite_analysis_common.mq_schemas import TaskMessage
from app import handler, work_agent


class BatchDispatchTests(unittest.TestCase):
    def test_mq_dispatches_group_job(self):
        task = TaskMessage(
            task_id="task",
            type="satellite_batch",
            land_id="A",
            extras={"job_id": "job"},
        )
        with patch.object(
            handler.celery_client,
            "send_task",
            return_value=SimpleNamespace(id="celery"),
        ) as send:
            result = handler._dispatch_satellite_batch(task, "A")
        self.assertEqual(
            send.call_args.args, ("app.tasks.satellite_batch.process_satellite_batch",)
        )
        self.assertEqual(
            send.call_args.kwargs["kwargs"], {"job_id": "job", "mq_task_id": "task"}
        )
        self.assertEqual(result["celery_ids"], ["celery"])

    def test_claim_reuses_group_dispatch(self):
        self.assertIn("satellite_batch", work_agent.DEFAULT_TYPES)
        self.assertIn("satellite_batch", work_agent.COMPLETE_ON_DISPATCH_TYPES)
        with patch.object(
            handler,
            "_dispatch_satellite_batch",
            return_value={"celery_ids": ["celery"]},
        ) as dispatch:
            result = work_agent._dispatch_celery(
                {
                    "id": "work",
                    "type": "satellite_batch",
                    "payload_json": {
                        "land_id": "A",
                        "task_id": "task",
                        "extras": {"job_id": "job"},
                    },
                }
            )
        self.assertEqual(dispatch.call_args.args[0].extras, {"job_id": "job"})
        self.assertEqual(result["work_item_id"], "work")


if __name__ == "__main__":
    unittest.main()
