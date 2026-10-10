"""一次性回填：旧版 S2 产品的预览地址从 OSS 产品 JSON 写入数据库列。

背景：``GET /lands/{id}/scenes?include_media=1`` 只读数据库列（rgb_url / large_rgb_url / rgb_oss_key），
不再为每景下载整份旧 OSS JSON。本脚本把旧 JSON 中的 rgb_url / large_rgb_url 一次性补到数据库，
之后这些产品即可走快速路径。

约束：
- 默认 dry-run，只统计不写库；加 ``--apply`` 才会提交。
- 只补 rgb_url / large_rgb_url 为空的行（COALESCE），不改 rgb_oss_key，不触碰像元数据。
- 与接口回退逻辑使用同一套解析函数（_load_oss_scene_json / _extract_oss_media_urls），结果一致。
- 旧 JSON 中的预览地址若为历史签名链接，回填后仍会随原链接过期，行为与改造前相同。

用法（在 services/api 目录下执行）：
    python -m scripts.backfill_scene_media_urls --land-id 61257
    python -m scripts.backfill_scene_media_urls --land-id 61257 --apply
    python -m scripts.backfill_scene_media_urls --apply --limit 2000
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from sqlalchemy import text

from app.core.database import async_session
from app.routers.agri import _extract_oss_media_urls, _load_oss_scene_json

logger = logging.getLogger("backfill_scene_media_urls")

_SELECT_SQL = text(
    """
    SELECT land_id, date, sensor, scene_id, json_oss_key
    FROM agric_satellite.parcel_scene_products
    WHERE sensor = 'S2'
      AND (CAST(:land_id AS text) IS NULL OR land_id = :land_id)
      AND rgb_url IS NULL
      AND large_rgb_url IS NULL
      AND rgb_oss_key IS NULL
      AND json_oss_key IS NOT NULL
    ORDER BY land_id, date, scene_id
    LIMIT :limit
    """
)

_UPDATE_SQL = text(
    """
    UPDATE agric_satellite.parcel_scene_products
    SET rgb_url = COALESCE(rgb_url, :rgb_url),
        large_rgb_url = COALESCE(large_rgb_url, :large_rgb_url)
    WHERE land_id = :land_id AND date = :date AND sensor = :sensor AND scene_id = :scene_id
    """
)

_COMMIT_EVERY = 50


async def _run(land_id: str | None, apply: bool, limit: int) -> dict[str, int]:
    stats = {"candidates": 0, "updated": 0, "no_json": 0, "no_media": 0}
    async with async_session() as session:
        rows = (
            await session.execute(
                _SELECT_SQL, {"land_id": land_id, "limit": limit}
            )
        ).mappings().all()
        stats["candidates"] = len(rows)
        pending = 0
        for row in rows:
            # 旧 JSON 读取是同步 OSS 调用，放入线程池，避免阻塞事件循环。
            obj = await asyncio.to_thread(_load_oss_scene_json, row["json_oss_key"])
            if obj is None:
                stats["no_json"] += 1
                continue
            media: dict[str, Any] = _extract_oss_media_urls(obj)
            rgb_url = media.get("rgb_url")
            large_rgb_url = media.get("large_rgb_url")
            if not rgb_url and not large_rgb_url:
                stats["no_media"] += 1
                continue
            if apply:
                await session.execute(
                    _UPDATE_SQL,
                    {
                        "rgb_url": rgb_url,
                        "large_rgb_url": large_rgb_url,
                        "land_id": row["land_id"],
                        "date": row["date"],
                        "sensor": row["sensor"],
                        "scene_id": row["scene_id"],
                    },
                )
                pending += 1
                if pending >= _COMMIT_EVERY:
                    await session.commit()
                    pending = 0
            stats["updated"] += 1
        if apply:
            await session.commit()
        else:
            await session.rollback()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="回填旧 S2 产品的预览地址到数据库列")
    parser.add_argument("--land-id", default=None, help="只处理指定地块；缺省处理全部")
    parser.add_argument("--limit", type=int, default=500, help="单次最多处理的候选行数")
    parser.add_argument("--apply", action="store_true", help="真正写库；缺省为 dry-run")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    stats = asyncio.run(_run(args.land_id, args.apply, args.limit))
    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info(
        "[%s] candidates=%s updated=%s no_json=%s no_media=%s",
        mode,
        stats["candidates"],
        stats["updated"],
        stats["no_json"],
        stats["no_media"],
    )


if __name__ == "__main__":
    main()
