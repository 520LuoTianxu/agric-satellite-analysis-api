"""带有限网络等待时间的STAC客户端工厂。"""

from __future__ import annotations

from typing import Any, Callable

STAC_REQUEST_TIMEOUT = (10.0, 60.0)


def open_stac_client(
    url: str,
    *,
    modifier: Callable[[Any], None] | None = None,
) -> Any:
    """打开STAC目录并为目录页、搜索页统一设置连接和读取超时。

    多页历史查询会连续等待远端响应；没有读取超时时，目录故障可能长期占住Celery worker。
    """
    from pystac_client import Client

    return Client.open(url, modifier=modifier, timeout=STAC_REQUEST_TIMEOUT)
