"""storage 服务的 Celery 入口。

只监听 ``storage`` 队列。任务实现在 ``app.tasks.storage_tasks``，
任务名必须保持 ``app.tasks.storage.*``，否则 API / ingest 的
``send_task`` 路由会对不上。
"""

from openfarm_common.celery_app import create_celery_app

# default_queue=storage：本进程发出的任务也进同一条队列，避免误投 ingest。
celery_app = create_celery_app(
    name="openfarm-storage",
    include=["app.tasks.storage_tasks"],
    default_queue="storage",
)
