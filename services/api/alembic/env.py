import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

# Ensure the project root is on sys.path so 'app' is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override URL from environment
db_url = os.environ.get("DATABASE_URL_SYNC") or os.environ.get("DATABASE_URL", "")
# Convert asyncpg URL to sync for Alembic
if "+asyncpg" in db_url:
    db_url = db_url.replace("+asyncpg", "")
config.set_main_option("sqlalchemy.url", db_url)

from app.models.tables import Base  # noqa: E402

target_metadata = Base.metadata

# 数据库只保留一个应用 schema；业务对象、扩展对象和 Alembic 版本表都在这里。
APP_DB_SCHEMA = "agric_satellite"


def include_name(name: str | None, type_: str, parent_names: dict[str, str]) -> bool:
    """让 Alembic 自动审计只关注唯一应用 schema。"""

    if type_ == "schema":
        return name == APP_DB_SCHEMA
    return True


def _prepare_online_database(connection) -> None:
    """为版本表迁移准备唯一 schema，并兼容 0023 遗留的 public 版本表。

    0023 已经把业务表搬到了 agric_satellite，但当时版本表仍在 public。
    必须在 Alembic 配置 MigrationContext 之前完成搬迁，否则当前迁移完成后
    Alembic 会继续尝试更新已经不存在的 public.alembic_version。
    """

    # 空数据库在 Alembic 创建版本表之前也必须先有目标 schema。
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {APP_DB_SCHEMA}"))
    connection.commit()

    legacy_exists, target_exists = connection.execute(
        text(
            """
            SELECT
                to_regclass('public.alembic_version') IS NOT NULL,
                to_regclass('agric_satellite.alembic_version') IS NOT NULL
            """
        )
    ).one()
    connection.commit()

    if legacy_exists and target_exists:
        raise RuntimeError(
            "数据库同时存在 public.alembic_version 和 "
            "agric_satellite.alembic_version，拒绝自动选择版本表"
        )
    if legacy_exists:
        # 单独提交这次目录变更，再由 Alembic 开启自己的迁移事务。
        connection.execute(
            text("ALTER TABLE public.alembic_version SET SCHEMA agric_satellite")
        )
        connection.commit()


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=APP_DB_SCHEMA,
        include_schemas=True,
        include_name=include_name,
    )
    # 离线 SQL 也要先创建 schema，否则版本表 DDL 会先于 0001 执行而失败。
    context.execute(f"CREATE SCHEMA IF NOT EXISTS {APP_DB_SCHEMA};")
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _prepare_online_database(connection)
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=APP_DB_SCHEMA,
            include_schemas=True,
            include_name=include_name,
        )
        with context.begin_transaction():
            # 放在 Alembic 事务内，避免 SQLAlchemy 预先自动开启事务后在连接关闭时回滚。
            connection.execute(text(f"SET LOCAL search_path TO {APP_DB_SCHEMA}"))
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
