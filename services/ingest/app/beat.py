"""下载机上的 Celery Beat 入口。

Beat 只往本机 Redis 发布任务名，不导入栅格任务，也不查 Postgres。
真正拉地块清单、执行下载的是 ingest worker。
"""

from agric_satellite_analysis_common.celery_app import create_celery_app

celery_app = create_celery_app(
    name="openfarm-ingest",
    include=[],
    default_queue="ingest",
    with_beat_schedule=True,
)
