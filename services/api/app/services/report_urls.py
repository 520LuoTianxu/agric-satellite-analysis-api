"""Generate browser-safe URLs for private OSS report objects."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.core.logging import logger
from app.core.storage import get_storage

# 报告链接只需要覆盖正常下载场景，不把私有 OSS 对象永久暴露给浏览器。
REPORT_URL_EXPIRES = timedelta(hours=24)


def signed_report_url(object_key: Any) -> str | None:
    """为报告对象生成短期签名 GET URL；失败时返回 None 交给 API 代理兜底。"""
    if not isinstance(object_key, str) or not object_key.strip():
        return None
    key = object_key.strip()
    try:
        return get_storage().presigned_get(key, expires=REPORT_URL_EXPIRES)
    except Exception as exc:
        # 不把 access key 或签名串写入日志；报告仍可通过 API 流式接口下载。
        logger.warning("report_signed_url_failed", object_key=key, error=str(exc))
        return None


def report_progress_for_response(
    progress: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """把响应里的报告 URL 替换为签名链接，不修改数据库原始 JSON。"""
    if not isinstance(progress, dict):
        return progress

    result = dict(progress)
    signed_url = signed_report_url(result.get("object_key"))
    if signed_url:
        # public_url 保持兼容旧前端；download_url 供新前端明确表示这是临时下载地址。
        result["public_url"] = signed_url
        result["download_url"] = signed_url
    elif result.get("object_key"):
        # 私有桶下未签名 URL 一定不可用，失败时移除它，让前端走 API 代理。
        result.pop("public_url", None)
        result.pop("download_url", None)
    return result


__all__ = ["REPORT_URL_EXPIRES", "report_progress_for_response", "signed_report_url"]
