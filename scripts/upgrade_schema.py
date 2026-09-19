#!/usr/bin/env python3
"""Run schema SQL outside the API container.

API images only start uvicorn. Database changes are applied with SQL files
under scripts/, never at container startup.

Usage:
  python scripts/upgrade_schema.py
  python scripts/upgrade_schema.py --dry-run
  python scripts/upgrade_schema.py --env-file ABflow/.env.test
  python scripts/upgrade_schema.py --sql scripts/convert_postgis_geometry_to_jsonb.sql
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SQL_FILE = ROOT / "scripts" / "convert_postgis_geometry_to_jsonb.sql"
DEFAULT_ENV_FILES = (
    ROOT / "ABflow" / ".env.test.local",
    ROOT / "ABflow" / ".env.test",
    ROOT / "services" / "api" / ".env",
    ROOT / ".env",
)


def _load_env_file(path: Path, *, override: bool) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if override or key not in os.environ:
                os.environ[key] = value
        return
    load_dotenv(path, override=override)


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL_SYNC") or os.environ.get("DATABASE_URL") or ""
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    url = url.replace("postgres+asyncpg://", "postgresql://")
    if not url:
        raise SystemExit(
            "DATABASE_URL_SYNC or DATABASE_URL is required. "
            "Pass --env-file or export the URL before running this script."
        )
    return url


def _safe_db_target(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or "<missing-host>"
    port = parsed.port or 5432
    database = (parsed.path or "/").lstrip("/") or "<default>"
    return f"{host}:{port}/{database}"


def _split_sql(sql: str) -> list[str]:
    """Split a SQL file into executable statements.

    Keep PL/pgSQL DO blocks intact. Skip the trailing inspection SELECT so a
    successful conversion still exits 0.
    """

    statements: list[str] = []
    buffer: list[str] = []
    in_do = False
    for raw_line in sql.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not in_do and (
            stripped.startswith("--") or stripped == "" or stripped == "BEGIN;"
        ):
            continue
        if stripped == "COMMIT;":
            continue
        if stripped.startswith("DO $upgrade$"):
            in_do = True
        # 只忽略文件末尾的检查 SELECT；UPDATE/INSERT 子查询中的 SELECT
        # 必须保留，否则包含 JSON 回填逻辑的升级脚本会被截断。
        if not in_do and not buffer and stripped.upper().startswith("SELECT"):
            break
        buffer.append(line)
        if in_do and stripped == "$upgrade$;":
            statements.append("\n".join(buffer).strip())
            buffer = []
            in_do = False
            continue
        if (
            not in_do
            and buffer
            and stripped.endswith(";")
            and not stripped.upper().startswith("SELECT")
        ):
            statements.append("\n".join(buffer).strip())
            buffer = []
    leftover = "\n".join(buffer).strip()
    if leftover and not leftover.upper().startswith("SELECT"):
        statements.append(leftover)
    return statements


def _inspect_sql() -> str:
    return """
        SELECT
            n.nspname AS schema_name,
            c.relname AS table_name,
            a.attname AS column_name,
            t.typname AS type_name
        FROM pg_catalog.pg_attribute AS a
        JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
        WHERE n.nspname IN ('agric_satellite', 'public', 'agri')
          AND t.typname IN ('geometry', 'geography')
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY 1, 2, 3
    """


def _connect(url: str):
    try:
        from sqlalchemy import create_engine
    except ImportError as exc:
        raise SystemExit(
            "sqlalchemy is required. Install services/api dependencies first."
        ) from exc
    return create_engine(url)


def _print_rows(title: str, rows: list[tuple]) -> None:
    print(title)
    if not rows:
        print("  (none)")
        return
    for schema_name, table_name, column_name, type_name in rows:
        print(f"  {schema_name}.{table_name}.{column_name} ({type_name})")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply schema SQL outside the API container."
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Optional dotenv file. Defaults to ABflow/.env.test.local, "
        "ABflow/.env.test, services/api/.env, then repo .env.",
    )
    parser.add_argument(
        "--sql",
        type=Path,
        default=SQL_FILE,
        help="SQL file to apply. Defaults to convert_postgis_geometry_to_jsonb.sql.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the database target and leftover spatial columns, then exit.",
    )
    args = parser.parse_args()

    if args.env_file:
        _load_env_file(args.env_file, override=False)
    else:
        for path in DEFAULT_ENV_FILES:
            if path.exists():
                _load_env_file(path, override=False)
                print(f"loaded env file: {path}")
                break

    url = _database_url()
    print(f"database: {_safe_db_target(url)}")

    engine = _connect(url)
    inspect = _inspect_sql()
    with engine.connect() as conn:
        before = list(conn.exec_driver_sql(inspect).fetchall())
    _print_rows("spatial columns before:", before)

    if args.dry_run:
        return 0

    sql_path = args.sql if args.sql.is_absolute() else ROOT / args.sql
    if not sql_path.exists():
        raise SystemExit(f"SQL file not found: {sql_path}")

    sql_text = sql_path.read_text(encoding="utf-8")
    statements = _split_sql(sql_text)
    convert_statements = [
        statement
        for statement in statements
        if not statement.upper().startswith("DROP EXTENSION")
    ]
    drop_statements = [
        statement
        for statement in statements
        if statement.upper().startswith("DROP EXTENSION")
    ]

    with engine.begin() as conn:
        for statement in convert_statements:
            preview = " ".join(statement.split())[:80]
            print(f"executing: {preview}")
            conn.exec_driver_sql(statement)

    with engine.connect() as conn:
        after = list(conn.exec_driver_sql(inspect).fetchall())
    _print_rows("spatial columns after:", after)
    if after:
        print(
            "conversion left spatial columns behind; "
            "do not drop PostGIS until those columns are converted.",
            file=sys.stderr,
        )
        return 1

    for statement in drop_statements:
        preview = " ".join(statement.split())[:80]
        print(f"executing: {preview}")
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(statement)
        except Exception as exc:
            print(f"warning: {preview} failed: {exc}", file=sys.stderr)

    print("schema SQL complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
