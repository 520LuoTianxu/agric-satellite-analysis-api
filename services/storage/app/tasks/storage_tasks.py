"""对象存储上传任务（仅 ``storage`` 队列）。

任务名固定为 ``app.tasks.storage.*``，与 API / ingest 的 ``send_task``
以及 ``agric_satellite_analysis_common.celery_app.TASK_ROUTES`` 对齐。实际 OSS 读写走
``agric_satellite_analysis_common.storage.get_storage()``，本文件只做队列侧契约。
"""

from __future__ import annotations

import base64
import os

from agric_satellite_analysis_common.logging import logger
from agric_satellite_analysis_common.storage import get_storage

from app.worker import celery_app


def _result_payload(key: str) -> dict:
    """上传成功后的统一返回：对象 key、公开 URL、后端名、内部 URI。"""
    storage = get_storage()
    return {
        "key": key,
        "public_url": storage.public_url(key),
        "backend": storage.backend,
        "uri": storage.uri_for(key),
    }


@celery_app.task(name="app.tasks.storage.upload_file", bind=True, max_retries=2)
def upload_file(
    self,
    key: str,
    path: str,
    content_type: str | None = None,
) -> dict:
    """把共享 scratch 卷上的本地文件上传到对象存储。

    ingest 把 COG / PDF 等产物写到 ``OPENFARM_SCRATCH_DIR``（默认
    ``/data/scratch``），再投递本任务。上传由本 worker 负责清理临时文件，
    避免 ingest 在等待结果时崩溃导致两边争着删文件。
    """
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f"upload path missing or not a file: {path!r}")
    storage = get_storage()
    try:
        storage.upload_file(key, path, content_type=content_type)
    finally:
        # 只清理 scratch 卷上的暂存文件；非 scratch 路径（调试用本地文件）不动。
        # rmdir 失败直接忽略：目录里可能还有同批其它文件。
        scratch_root = os.environ.get("OPENFARM_SCRATCH_DIR", "/data/scratch")
        try:
            if path.startswith(scratch_root.rstrip("/") + "/") and os.path.isfile(path):
                parent = os.path.dirname(path)
                os.unlink(path)
                try:
                    os.rmdir(parent)
                except OSError:
                    pass
        except OSError:
            pass
    logger.info(
        "storage_task_upload_file",
        key=key,
        path=path,
        backend=storage.backend,
    )
    return _result_payload(key)


@celery_app.task(name="app.tasks.storage.put_bytes", bind=True, max_retries=2)
def put_bytes(
    self,
    key: str,
    data_b64: str,
    content_type: str | None = None,
) -> dict:
    """上传小对象（payload 为 base64）。

    走 Redis broker 传字节，只适合 JSON、缩略图等小文件。COG 等大文件
    必须走 ``upload_file``（共享卷），否则会撑爆 broker。
    """
    data = base64.b64decode(data_b64)
    storage = get_storage()
    storage.put_bytes(key, data, content_type=content_type)
    logger.info(
        "storage_task_put_bytes",
        key=key,
        bytes=len(data),
        backend=storage.backend,
    )
    return _result_payload(key)


@celery_app.task(name="app.tasks.storage.exists")
def exists(key: str) -> bool:
    """查询对象存储里是否已有该 key。"""
    return bool(get_storage().exists(key))


@celery_app.task(name="app.tasks.storage.public_url")
def public_url(key: str) -> str:
    """按当前后端规则拼公开访问 URL，不访问网络。"""
    return get_storage().public_url(key)


__all__ = ["upload_file", "put_bytes", "exists", "public_url"]
