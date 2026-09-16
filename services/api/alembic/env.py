import os
import socket
import sys
from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool, text
from sqlalchemy.engine import make_url

# Ensure the project root is on sys.path so 'app' is importable
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)
# ABflow 将测试配置复制为项目根目录 .env；迁移脚本需要先加载它才能读取同步数据库连接串。
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=False)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override URL from environment
db_url = os.environ.get("DATABASE_URL_SYNC") or os.environ.get("DATABASE_URL", "")
# Convert asyncpg URL to sync for Alembic
if "+asyncpg" in db_url:
    db_url = db_url.replace("+asyncpg", "")


# 容器内无法交互登录时，先输出脱敏的 DNS 和 TCP 结果，区分网络故障与数据库认证故障。
def _log_database_network_diagnostics(url: str) -> None:
    """在 Alembic 连接数据库前输出容器网络诊断信息，且不泄露连接凭据。"""

    try:
        parsed_url = make_url(url)
    except Exception:
        print("[DB诊断] 数据库连接串无法解析。", flush=True)
        return

    host = parsed_url.host
    port = parsed_url.port or 5432
    database = parsed_url.database or "<default>"
    print(
        f"[DB诊断] host={host or '<missing>'}, port={port}, database={database}",
        flush=True,
    )

    try:
        with open("/etc/resolv.conf", encoding="utf-8") as resolv_file:
            resolv_conf = resolv_file.read().strip()
        print(f"[DB诊断] /etc/resolv.conf:\n{resolv_conf or '<empty>'}", flush=True)
    except OSError as exc:
        print(
            f"[DB诊断] 无法读取 /etc/resolv.conf: {type(exc).__name__}: {exc}",
            flush=True,
        )

    if not host:
        print("[DB诊断] 数据库 host 未配置，跳过 DNS 和 TCP 检查。", flush=True)
        return

    try:
        address_infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        print(f"[DB诊断] DNS 解析失败: {type(exc).__name__}: {exc}", flush=True)
        return

    endpoints = []
    seen_endpoints = set()
    for family, socktype, proto, _, sockaddr in address_infos:
        endpoint_key = (family, sockaddr)
        if endpoint_key in seen_endpoints:
            continue
        seen_endpoints.add(endpoint_key)
        endpoints.append((family, socktype, proto, sockaddr))

    addresses = ", ".join(str(sockaddr[0]) for _, _, _, sockaddr in endpoints)
    print(f"[DB诊断] DNS 解析成功: {addresses}", flush=True)

    last_error = None
    for family, socktype, proto, sockaddr in endpoints:
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(5)
                sock.connect(sockaddr)
            print(f"[DB诊断] TCP {sockaddr[0]}:{sockaddr[1]} 连接成功。", flush=True)
            return
        except OSError as exc:
            last_error = exc

    if last_error is not None:
        print(
            f"[DB诊断] TCP 连接失败: {type(last_error).__name__}: {last_error}",
            flush=True,
        )


_log_database_network_diagnostics(db_url)
# ConfigParser 会将百分号当作插值标记；密码中的 URL 编码（如 %23）需要先转义。
config.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))

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
