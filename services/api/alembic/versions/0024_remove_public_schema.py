"""Move ordinary PostgreSQL extensions into ``agric_satellite``.

The previous migration unified application relations but deliberately left the
Alembic version table in ``public``.  This migration finishes the namespace
cut-over without copying table data.  Spatial extensions are intentionally not
created or relocated; application geometry is stored as JSONB instead.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels = None
depends_on = None

APP_SCHEMA = "agric_satellite"
PUBLIC_SCHEMA = "public"


def _quote(bind: sa.engine.Connection, identifier: str) -> str:
    """Quote a catalog identifier before composing an extension statement."""

    return bind.dialect.identifier_preparer.quote(identifier)


def _extension_info(
    bind: sa.engine.Connection, extension_name: str
) -> tuple[str, str, bool] | None:
    row = bind.execute(
        sa.text(
            """
            SELECT n.nspname, e.extversion, e.extrelocatable
            FROM pg_catalog.pg_extension AS e
            JOIN pg_catalog.pg_namespace AS n ON n.oid = e.extnamespace
            WHERE e.extname = :extension_name
            """
        ),
        {"extension_name": extension_name},
    ).one_or_none()
    if row is None:
        return None
    return str(row[0]), str(row[1]), bool(row[2])


def _installed_in_schema(bind: sa.engine.Connection, schema: str) -> list[str]:
    rows = bind.execute(
        sa.text(
            """
            SELECT e.extname
            FROM pg_catalog.pg_extension AS e
            JOIN pg_catalog.pg_namespace AS n ON n.oid = e.extnamespace
            WHERE n.nspname = :schema
            ORDER BY e.extname
            """
        ),
        {"schema": schema},
    ).scalars()
    return [str(name) for name in rows]


def _move_relocatable_extension(
    bind: sa.engine.Connection,
    extension_name: str,
    source_schema: str,
    target_schema: str,
) -> None:
    info = _extension_info(bind, extension_name)
    if info is None or info[0] == target_schema:
        return
    current_schema, _current_version, relocatable = info
    if current_schema != source_schema:
        raise RuntimeError(
            f"extension {extension_name} is installed in unexpected schema "
            f"{current_schema}; expected {source_schema} or {target_schema}"
        )
    if not relocatable:
        raise RuntimeError(
            f"extension {extension_name} is non-relocatable and is not covered "
            "by the generic extension relocation procedure"
        )
    bind.execute(
        sa.text(
            f"ALTER EXTENSION {_quote(bind, extension_name)} SET SCHEMA "
            f"{_quote(bind, target_schema)}"
        )
    )


def _move_extensions(
    bind: sa.engine.Connection, source_schema: str, target_schema: str
) -> None:
    # 应用只保留普通 PostgreSQL 扩展；几何边界不再依赖数据库空间扩展。
    installed = _installed_in_schema(bind, source_schema)
    for extension_name in installed:
        _move_relocatable_extension(bind, extension_name, source_schema, target_schema)


def upgrade() -> None:
    bind = op.get_bind()
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {APP_SCHEMA}")
    op.execute("SET LOCAL lock_timeout = '30s'")
    op.execute(f"SET LOCAL search_path TO {APP_SCHEMA}")

    # env.py 已在 MigrationContext 初始化前将 public.alembic_version 搬入目标 schema。
    _move_extensions(bind, PUBLIC_SCHEMA, APP_SCHEMA)

    # RESTRICT 是最后一道安全闸：任何未盘点的 public 对象都会让整笔迁移回滚。
    op.execute("DROP SCHEMA IF EXISTS public RESTRICT")


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("CREATE SCHEMA IF NOT EXISTS public")
    op.execute("SET LOCAL lock_timeout = '30s'")
    op.execute(f"SET LOCAL search_path TO {APP_SCHEMA}")

    _move_extensions(bind, APP_SCHEMA, PUBLIC_SCHEMA)
    # 版本表继续留在 agric_satellite，避免在本次 downgrade 中删除 Alembic 当前上下文。
