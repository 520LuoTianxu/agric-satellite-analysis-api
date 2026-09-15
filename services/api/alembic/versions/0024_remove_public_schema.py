"""Move extensions into ``agric_satellite`` and remove PostgreSQL ``public``.

The previous migration unified application relations but deliberately left the
PostGIS objects and Alembic version table in ``public``.  This migration
finishes the namespace cut-over without copying table data.  PostGIS is not
relocatable by default, so its documented temporary-relocation procedure is
used and the extension is reinstalled from its local SQL scripts in place.
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels = None
depends_on = None

APP_SCHEMA = "agric_satellite"
PUBLIC_SCHEMA = "public"
POSTGIS_EXTENSION = "postgis"
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9_.-]+$")


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
            ORDER BY CASE WHEN e.extname = 'postgis' THEN 0 ELSE 1 END, e.extname
            """
        ),
        {"schema": schema},
    ).scalars()
    return [str(name) for name in rows]


def _dummy_postgis_version(
    bind: sa.engine.Connection, extension_name: str, current_version: str
) -> str:
    """Find the local PostGIS dummy version needed to force a reinstall."""

    next_version = f"{current_version}next"
    versions = bind.execute(
        sa.text(
            """
            SELECT version
            FROM pg_catalog.pg_available_extension_versions
            WHERE name = :extension_name
              AND version IN ('ANY', :next_version)
            ORDER BY CASE WHEN version = 'ANY' THEN 0 ELSE 1 END
            """
        ),
        {"extension_name": extension_name, "next_version": next_version},
    ).scalars()
    candidates = [str(version) for version in versions]
    if candidates:
        return candidates[0]
    raise RuntimeError(
        f"{extension_name} {current_version} cannot be relocated: "
        f"local package has neither ANY nor {next_version} upgrade script"
    )


def _update_extension_in_place(
    bind: sa.engine.Connection, extension_name: str, current_version: str
) -> None:
    """Reinstall extension functions so @extschema@ points at the new schema."""

    dummy_version = _dummy_postgis_version(bind, extension_name, current_version)
    if not _SAFE_VERSION.fullmatch(dummy_version):
        raise RuntimeError(f"unsafe extension version returned: {dummy_version!r}")
    quoted_name = _quote(bind, extension_name)
    quoted_version = _quote(bind, dummy_version)
    bind.execute(sa.text(f"ALTER EXTENSION {quoted_name} UPDATE TO {quoted_version}"))
    bind.execute(sa.text(f"ALTER EXTENSION {quoted_name} UPDATE"))


def _move_postgis_extension(
    bind: sa.engine.Connection,
    extension_name: str,
    source_schema: str,
    target_schema: str,
) -> None:
    info = _extension_info(bind, extension_name)
    if info is None or info[0] == target_schema:
        return
    current_schema, current_version, relocatable = info
    if current_schema != source_schema:
        raise RuntimeError(
            f"extension {extension_name} is installed in unexpected schema "
            f"{current_schema}; expected {source_schema} or {target_schema}"
        )

    # PostGIS >= 2.3 marks itself non-relocatable.  The official PostGIS
    # procedure temporarily flips this catalog flag, moves the extension, and
    # forces a reinstall so every @extschema@ reference follows the new schema.
    if not relocatable:
        bind.execute(
            sa.text(
                "UPDATE pg_catalog.pg_extension "
                "SET extrelocatable = true WHERE extname = :extension_name"
            ),
            {"extension_name": extension_name},
        )
    quoted_name = _quote(bind, extension_name)
    bind.execute(
        sa.text(
            f"ALTER EXTENSION {quoted_name} SET SCHEMA {_quote(bind, target_schema)}"
        )
    )
    _update_extension_in_place(bind, extension_name, current_version)
    # Restore PostGIS's normal non-relocatable safety guard after the move.
    bind.execute(
        sa.text(
            "UPDATE pg_catalog.pg_extension "
            "SET extrelocatable = false WHERE extname = :extension_name"
        ),
        {"extension_name": extension_name},
    )


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
            "by the PostGIS relocation procedure"
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
    # PostGIS 及其可选组件必须先按 PostGIS 流程移动，再处理普通可迁移扩展。
    installed = _installed_in_schema(bind, source_schema)
    for extension_name in installed:
        if extension_name == POSTGIS_EXTENSION or extension_name.startswith("postgis_"):
            _move_postgis_extension(bind, extension_name, source_schema, target_schema)
    for extension_name in installed:
        if extension_name != POSTGIS_EXTENSION and not extension_name.startswith(
            "postgis_"
        ):
            _move_relocatable_extension(
                bind, extension_name, source_schema, target_schema
            )


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
