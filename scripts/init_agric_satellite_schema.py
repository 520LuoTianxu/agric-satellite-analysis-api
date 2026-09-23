"""在生产发布阶段一次性初始化 agric_satellite 数据库结构。

用法：
    python scripts/init_agric_satellite_schema.py
    python scripts/init_agric_satellite_schema.py --sql ABflow/agric_satellite.sql

脚本不会在 API 容器启动时自动执行，必须由发布流程显式调用。
DATABASE_URL_SYNC / DATABASE_URL 存在时优先使用环境变量；常量仅作为当前
发布配置的默认连接信息。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQL_FILE = ROOT / "ABflow" / "agric_satellite.sql"
PROJECT_AREA_CLEANUP_SQL = (
    ROOT / "scripts" / "agri_seed" / "005_remove_virtual_project_areas.sql"
)
DATABASE_SCHEMA = "agric_satellite"

# 按当前发布配置保留默认连接；生产环境建议通过安全变量覆盖，避免凭据进入日志或镜像。
DEFAULT_DATABASE_URL = (
    "postgresql://agric:Zhnx%23Agric%40P_g%21P@"
    "pgm-2ze0zej93987p02i.pg.rds.aliyuncs.com:5432/agri_mate?sslmode=disable"
)

# 初始化完成后必须同时存在这些对象；只存在其中一部分时视为异常，避免误跳过半初始化数据库。
REQUIRED_OBJECTS = (
    "admin_task_runs",
    "alembic_version",
    "land_parcels",
    "work_items",
    "v_land_parcels_detail",
)


def _database_url() -> str:
    """优先读取发布环境注入的连接串，未注入时回退到当前固定配置。"""
    return (
        os.environ.get("DATABASE_URL_SYNC")
        or os.environ.get("DATABASE_URL")
        or DEFAULT_DATABASE_URL
    ).replace("postgresql+asyncpg://", "postgresql://")


def _object_state(connection) -> dict[str, bool]:
    """查询初始化对象状态，区分已完成、未开始和部分完成三种情况。"""
    state: dict[str, bool] = {}
    for object_name in REQUIRED_OBJECTS:
        qualified_name = f"{DATABASE_SCHEMA}.{object_name}"
        state[object_name] = connection.execute(
            text("SELECT to_regclass(:qualified_name) IS NOT NULL"),
            {"qualified_name": qualified_name},
        ).scalar_one()
    return state


def _apply_project_area_cleanup(connection) -> bool:
    """前向清理旧导出中随 schema 一起保留的项目区表和视图。"""
    if not PROJECT_AREA_CLEANUP_SQL.is_file():
        return False
    cleanup_sql = PROJECT_AREA_CLEANUP_SQL.read_text(encoding="utf-8")
    cleanup_sql = cleanup_sql.replace("BEGIN;", "").replace("COMMIT;", "")
    connection.exec_driver_sql(cleanup_sql)
    return True


def initialize_schema(sql_file: Path) -> str:
    """在单个事务中执行 SQL，并校验核心表和视图已创建。"""
    if not sql_file.is_file():
        raise FileNotFoundError(f"SQL 文件不存在: {sql_file}")

    sql_text = sql_file.read_text(encoding="utf-8")
    if not sql_text.strip():
        raise ValueError(f"SQL 文件为空: {sql_file}")

    engine = create_engine(
        _database_url(),
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 15,
            # SQL 中视图定义包含未限定表名，固定 search_path 保证引用当前业务 schema。
            "options": f"-csearch_path={DATABASE_SCHEMA}",
        },
    )
    try:
        with engine.begin() as connection:
            # 发布可能被重复触发；事务级锁保证同一初始化不会并发执行。
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": "agric_satellite.schema.init.v1"},
            )

            schema_exists = connection.execute(
                text("SELECT to_regnamespace(:schema_name) IS NOT NULL"),
                {"schema_name": DATABASE_SCHEMA},
            ).scalar_one()
            if not schema_exists:
                raise RuntimeError(
                    f"数据库中不存在 schema {DATABASE_SCHEMA}，请先创建并授权"
                )

            state = _object_state(connection)
            existing = [name for name, exists in state.items() if exists]
            if len(existing) == len(REQUIRED_OBJECTS):
                # 已初始化的数据库也必须执行幂等清理迁移，不能因跳过 schema 导入而保留旧表。
                return (
                    "already_initialized_cleaned"
                    if _apply_project_area_cleanup(connection)
                    else "already_initialized"
                )
            if existing:
                raise RuntimeError(
                    "检测到部分初始化对象，已停止执行，请人工检查: "
                    + ", ".join(existing)
                )

            # exec_driver_sql 保留原始 PostgreSQL DDL，支持函数体、视图和多条建表语句。
            connection.exec_driver_sql(sql_text)
            # 旧导出快照仍包含遗留表；初始化结束时执行清理迁移，保持运行时 schema 一致。
            _apply_project_area_cleanup(connection)

            final_state = _object_state(connection)
            missing = [name for name, exists in final_state.items() if not exists]
            if missing:
                raise RuntimeError(
                    "SQL 执行后缺少核心对象: " + ", ".join(missing)
                )
            return "initialized"
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sql",
        type=Path,
        default=DEFAULT_SQL_FILE,
        help="初始化 SQL 文件，默认使用 ABflow/agric_satellite.sql",
    )
    args = parser.parse_args()
    sql_file = args.sql if args.sql.is_absolute() else ROOT / args.sql

    try:
        result = initialize_schema(sql_file)
    except Exception as exc:  # noqa: BLE001 - 发布脚本需要统一输出失败原因。
        print(f"数据库初始化失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if result == "already_initialized":
        print(f"数据库已初始化，跳过执行: {DATABASE_SCHEMA}")
    elif result == "already_initialized_cleaned":
        print(f"数据库已初始化，并已执行遗留项目区清理迁移: {DATABASE_SCHEMA}")
    else:
        print(f"数据库初始化完成: {DATABASE_SCHEMA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
