from __future__ import annotations

from unittest.mock import MagicMock, patch

from agric_satellite_analysis_common.mq_schemas import TaskMessage


def test_handler_dispatches_biweekly_report_task():
    from app.handler import _dispatch_biweekly_report

    task = TaskMessage(
        task_id="t1",
        type="biweekly_report",
        land_id="19994",
        extras={
            "job_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "biweekly_job_id": "bj-test",
            "date_from": "2025-11-27",
            "date_to": "2025-12-10",
            "wait_for_job_ids": ["batch-1"],
        },
    )
    sent = MagicMock()
    sent.id = "celery-1"
    with patch("app.handler._send_task", return_value=sent) as send:
        info = _dispatch_biweekly_report(task, "19994")
    assert info["dispatched"] == ["app.tasks.biweekly_report.generate_biweekly_report"]
    assert info["biweekly_job_id"] == "bj-test"
    kwargs = send.call_args.kwargs["kwargs"]
    assert kwargs["biweekly_job_id"] == "bj-test"
    assert kwargs["wait_for_job_ids"] == ["batch-1"]
    assert kwargs["report_job_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def test_work_agent_dispatch_report_uses_biweekly_task():
    from app.work_agent import _dispatch_report

    sent = MagicMock()
    sent.id = "celery-2"
    with patch("app.work_agent.celery_client") as client:
        client.send_task.return_value = sent
        info = _dispatch_report(
            "biweekly_report",
            "work-1",
            "6417",
            {
                "job_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "biweekly_job_id": "bj-abc",
                "date_from": "2026-04-04",
                "date_to": "2026-04-17",
            },
        )
    assert info["dispatched"] == ["app.tasks.biweekly_report.generate_biweekly_report"]
    kwargs = client.send_task.call_args.kwargs["kwargs"]
    assert kwargs["biweekly_job_id"] == "bj-abc"
    assert kwargs["report_job_id"] == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    assert kwargs["work_item_id"] == "work-1"


def test_work_agent_passes_work_item_oss_artifact_to_worker():
    from app.work_agent import _dispatch_report

    sent = MagicMock()
    sent.id = "celery-3"
    oss = {
        "bucket": "agric-test",
        "key": "reports/default/6417/biweekly-bj-aaaaaaaaaaaaaaaa.pdf",
        "size_bytes": 2048,
        "sha256": "a" * 64,
        "content_type": "application/pdf",
    }
    item = {
        "id": "work-2",
        "type": "biweekly_report",
        "payload_json": {
            "land_id": "6417",
            "extras": {
                "job_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                "biweekly_job_id": "bj-aaaaaaaaaaaaaaaa",
            },
        },
        "progress_json": {
            "stage": "registration_pending",
            "official_status": "ready",
            "filename": "x.pdf",
            "oss_artifact": oss,
            "registration_attempts": 8,
        },
    }
    with patch("app.work_agent.celery_client") as client:
        client.send_task.return_value = sent
        _dispatch_report(
            "biweekly_report",
            "work-2",
            "6417",
            item["payload_json"]["extras"],
            item=item,
        )
    kwargs = client.send_task.call_args.kwargs["kwargs"]
    assert kwargs["registration_progress"]["oss_artifact"]["sha256"] == "a" * 64
    assert kwargs["registration_progress"]["oss_artifact"]["key"] == oss["key"]
    assert kwargs["report_job_id"] == "cccccccc-cccc-cccc-cccc-cccccccccccc"


def test_work_agent_omits_registration_progress_when_work_item_has_no_oss():
    from app.work_agent import _dispatch_report

    sent = MagicMock()
    sent.id = "celery-4"
    with patch("app.work_agent.celery_client") as client:
        client.send_task.return_value = sent
        _dispatch_report(
            "biweekly_report",
            "work-3",
            "6417",
            {"job_id": "dddddddd-dddd-dddd-dddd-dddddddddddd", "biweekly_job_id": "bj-bbbbbbbbbbbbbbbb"},
            item={"id": "work-3", "payload_json": {}, "progress_json": {"stage": "waiting_for_backfill"}},
        )
    kwargs = client.send_task.call_args.kwargs["kwargs"]
    assert "registration_progress" not in kwargs
