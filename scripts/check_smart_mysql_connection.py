#!/usr/bin/env python3
"""只读检查 Smart MySQL 连接和指定地块是否可见。

Usage:
  $env:SMART_MYSQL_USERNAME = "agric"
  $env:SMART_MYSQL_PASSWORD = "..."
  python scripts/check_smart_mysql_connection.py --env-file ABflow/.env.test
  python scripts/check_smart_mysql_connection.py --land-id 61224 --land-id 61225

密码只从环境变量或交互式输入读取，不写入脚本，也不打印连接串。
脚本复用 API 的连接串兼容逻辑，并明确按非 SSL 方式创建 asyncmy 引擎。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = ROOT / "ABflow" / ".env.test"


def _load_env_file(path: Path) -> None:
    """加载指定测试配置，避免脚本误用当前 shell 中的旧数据库 URL。"""
    if not path.is_file():
        raise FileNotFoundError(f"env file not found: {path}")
    from dotenv import load_dotenv

    load_dotenv(path, override=True)


def _build_source_url(
    username: str | None,
    password: str,
    host: str | None,
    port: int | None,
) -> URL:
    """替换临时凭据并复用 API 的非 SSL asyncmy URL 适配逻辑。"""
    raw_url = os.environ.get("MYSQL_SOURCE_URL", "").strip()
    if not raw_url:
        raise RuntimeError("MYSQL_SOURCE_URL is required")

    # 脚本从仓库根目录运行时，显式加入 API 包路径以复用同一连接适配实现。
    api_root = ROOT / "services" / "api"
    sys.path.insert(0, str(api_root))
    from app.services.mysql_land_sync import _normalize_mysql_source_url

    base_url = make_url(raw_url)
    effective_username = (
        username or os.environ.get("SMART_MYSQL_USERNAME") or base_url.username
    )
    if not effective_username:
        raise RuntimeError("Smart MySQL username is required")
    if host:
        base_url = base_url.set(host=host)
    if port:
        base_url = base_url.set(port=port)
    return _normalize_mysql_source_url(
        base_url.set(username=effective_username, password=password)
    )


async def _check_connection(
    url: URL, land_ids: list[str], timeout: int
) -> None:
    """执行只读探活和地块存在性查询；不会写入 Smart 或 PostgreSQL。"""
    target = make_url(str(url))
    print(f"target={target.host}:{target.port or 3306}/{target.database}")
    print(f"username={target.username}")
    print("ssl=false")

    # URL 已在 _build_source_url 中移除 SSL/JDBC 专属参数；connect_args 同样不传 SSL。
    engine = create_async_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": timeout},
    )
    try:
        async with engine.connect() as conn:
            print("select_1=", (await conn.execute(text("SELECT 1 AS ok"))).scalar_one())
            count = (
                await conn.execute(text("SELECT COUNT(*) FROM agriculture_land"))
            ).scalar_one()
            print("agriculture_land_count=", count)

            if not land_ids:
                return

            placeholders = ", ".join(
                f":land_{index}" for index in range(len(land_ids))
            )
            result = await conn.execute(
                text(
                    "SELECT CAST(land_id AS CHAR) AS land_id "
                    "FROM agriculture_land "
                    f"WHERE CAST(land_id AS CHAR) IN ({placeholders}) "
                    "ORDER BY land_id"
                ),
                {f"land_{index}": value for index, value in enumerate(land_ids)},
            )
            visible = [str(row[0]) for row in result.fetchall()]
            print("requested_land_ids=", land_ids)
            print("visible_land_ids=", visible)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="dotenv file containing MYSQL_SOURCE_URL",
    )
    parser.add_argument("--username", default=None)
    parser.add_argument("--host", default=None, help="optional MySQL host override")
    parser.add_argument("--port", type=int, default=None, help="optional port override")
    parser.add_argument(
        "--password-env",
        default="SMART_MYSQL_PASSWORD",
        help="environment variable containing the password",
    )
    parser.add_argument(
        "--land-id",
        action="append",
        default=[],
        help="optional Smart land ID to check; may be repeated",
    )
    parser.add_argument("--timeout", type=int, default=10)
    args = parser.parse_args()

    try:
        _load_env_file(args.env_file)
        password = os.environ.get(args.password_env) or getpass.getpass(
            "Smart MySQL password: "
        )
        if not password:
            raise RuntimeError("Smart MySQL password is required")
        url = _build_source_url(args.username, password, args.host, args.port)
        asyncio.run(_check_connection(url, args.land_id, args.timeout))
    except Exception as exc:
        print(f"connection_failed={type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
