#!/usr/bin/env python3
"""Re-enqueue already-uploaded parcel scene JSONs into /internal/results/cache.

After the API machine has pulled current master (RESP2 Redis fix) and
restarted, run this so land 19994 products already on OSS enter the scene
cache queue and growth analysis can render. This is a re-POST of existing
OSS objects; it does not trigger a new 补齐 download.

Default is dry-run (list keys, print counts, no HTTP). --execute posts.

Environment (same as the download machine / ingest worker):
  API_BASE_URL, INTERNAL_API_TOKEN, and OSS credentials used by get_storage().
  API_PATH_PREFIX: omit or set empty when API_BASE_URL is the public
  satellite-api gateway (path is /internal/results/cache). Direct API
  deployments keep the default /v1 prefix.

Do not print tokens, Redis URLs, or OSS secrets.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any, Iterable

SCENE_KEY_RE = re.compile(r"^(?P<land_id>[^/]+)/(?P<date>\d{4}-\d{2}-\d{2})_(?P<sensor>[A-Za-z0-9]+)\.json$")
DEFAULT_LAND_ID = "19994"
DEFAULT_OSS_PREFIX = "s1s2_parcel/json/"


def normalize_prefix(prefix: str | None) -> str:
    value = (prefix or DEFAULT_OSS_PREFIX).strip() or DEFAULT_OSS_PREFIX
    return value if value.endswith("/") else f"{value}/"


def land_product_prefix(land_id: str, *, oss_prefix: str | None = None) -> str:
    return f"{normalize_prefix(oss_prefix)}{land_id}/"


def parse_scene_oss_key(key: str, *, oss_prefix: str | None = None) -> dict[str, str] | None:
    """Parse ``{prefix}{land_id}/{date}_{sensor}.json`` into land/date/sensor."""
    prefix = normalize_prefix(oss_prefix)
    remainder = key[len(prefix) :] if key.startswith(prefix) else key.lstrip("/")
    match = SCENE_KEY_RE.match(remainder)
    if not match:
        return None
    return {
        "json_oss_key": key,
        "land_id": match.group("land_id"),
        "date": match.group("date"),
        "sensor": match.group("sensor"),
    }


def build_cache_result(
    *,
    land_id: str,
    date: str,
    sensor: str,
    json_url: str,
    json_oss_key: str,
) -> dict[str, Any]:
    label = f"{date}_{sensor}"
    return {
        "status": "success",
        "oss_urls": {label: json_url},
        "extras": {
            "kind": "parcel_scene_product",
            "land_id": str(land_id),
            "sensor": sensor,
            "date": date,
            "json_oss_key": json_oss_key,
        },
    }


def list_land_scene_keys(storage: Any, land_id: str, *, oss_prefix: str | None = None) -> list[str]:
    prefix = land_product_prefix(land_id, oss_prefix=oss_prefix)
    keys = storage.list_keys(prefix, suffix=".json")
    parsed: list[str] = []
    for key in keys:
        info = parse_scene_oss_key(key, oss_prefix=oss_prefix)
        if info and info["land_id"] == str(land_id):
            parsed.append(key)
    return parsed


def envelopes_for_keys(
    storage: Any,
    keys: Iterable[str],
    *,
    oss_prefix: str | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for key in keys:
        info = parse_scene_oss_key(key, oss_prefix=oss_prefix)
        if not info:
            continue
        results.append(
            build_cache_result(
                land_id=info["land_id"],
                date=info["date"],
                sensor=info["sensor"],
                json_url=storage.public_url(key),
                json_oss_key=key,
            )
        )
    return results


def _post_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from agric_satellite_analysis_common.internal_api import cache_results

    replies: list[dict[str, Any]] = []
    for item in results:
        replies.append(cache_results(item, timeout=30.0, flush=True))
    return replies


def _env_ready_for_execute() -> tuple[bool, str]:
    from agric_satellite_analysis_common.internal_api import api_base_url, internal_api_token

    if not api_base_url():
        return False, "API_BASE_URL is not set"
    if not internal_api_token():
        return False, "INTERNAL_API_TOKEN is not set"
    return True, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--land-id", default=DEFAULT_LAND_ID)
    parser.add_argument(
        "--oss-prefix",
        default=os.environ.get("OSS_PREFIX", DEFAULT_OSS_PREFIX),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="POST each product to /internal/results/cache. Default is dry-run.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Post/list at most N keys (0 = all).")
    args = parser.parse_args(argv)

    from agric_satellite_analysis_common.storage import get_storage

    storage = get_storage()
    keys = list_land_scene_keys(storage, args.land_id, oss_prefix=args.oss_prefix)
    keys.sort()
    if args.limit and args.limit > 0:
        keys = keys[: args.limit]
    results = envelopes_for_keys(storage, keys, oss_prefix=args.oss_prefix)
    print(f"land_id={args.land_id} oss_json_count={len(keys)} envelopes={len(results)}")
    for item in results:
        extras = item["extras"]
        label = next(iter(item["oss_urls"]))
        print(f"  {label} key={extras['json_oss_key']}")

    if not args.execute:
        print("dry-run: no HTTP POST was sent")
        return 0

    ready, reason = _env_ready_for_execute()
    if not ready:
        print(f"execute aborted: {reason}", file=sys.stderr)
        return 2

    replies = _post_results(results)
    ok = 0
    failed = 0
    for reply in replies:
        if isinstance(reply, dict) and reply.get("ok") is not False:
            ok += 1
        else:
            failed += 1
    print(f"posted={len(replies)} accepted={ok} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
