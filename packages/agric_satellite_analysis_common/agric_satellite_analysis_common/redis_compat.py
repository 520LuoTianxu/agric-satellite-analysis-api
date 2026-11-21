"""Redis clients that stay on RESP2 and never send HELLO 3.

redis-py 5.x defaults to RESP3 and, when the URL has credentials, sends
``HELLO 3 AUTH ...``. Older Redis servers reject HELLO, so every client that
talks to the API-machine Redis must pass ``protocol=2`` explicitly. Do not
rely on the library default (it is 2 in redis-py 6.4, but 3 in 5.x).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import redis
import redis.asyncio as aioredis

# Explicit RESP2. Never omit this and never allow a URL query to select 3.
REDIS_RESP2_PROTOCOL = 2


def url_without_protocol_param(url: str) -> str:
    """Drop a ``protocol`` query parameter so it cannot override RESP2.

    The URL may contain a password; callers must not log it.
    """
    parts = urlsplit(url)
    if not parts.query:
        return url
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() != "protocol"
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def create_sync_redis_client(url: str, **kwargs: Any) -> Any:
    """Return a synchronous redis.Redis client pinned to RESP2 (no HELLO 3)."""
    kwargs["protocol"] = REDIS_RESP2_PROTOCOL
    return redis.Redis.from_url(url_without_protocol_param(url), **kwargs)


def create_async_redis_client(url: str, **kwargs: Any) -> Any:
    """Return a redis.asyncio client pinned to RESP2 (no HELLO 3)."""
    kwargs["protocol"] = REDIS_RESP2_PROTOCOL
    return aioredis.from_url(url_without_protocol_param(url), **kwargs)
