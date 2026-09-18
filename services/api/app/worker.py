"""API 进程的 Celery 客户端，只负责 send_task。

定时 Beat 在 ingest 镜像的 ``app.beat``。重任务模块在 ingest / storage，
本模块不得导入它们，路由按稳定任务名派单。
"""

from agric_satellite_analysis_common.celery_app import CPU_COMPUTE_QUEUE, create_celery_app

celery_app = create_celery_app(
    name="openfarm",
    include=[],
    default_queue=CPU_COMPUTE_QUEUE,
)
