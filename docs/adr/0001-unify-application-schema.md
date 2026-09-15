# ADR-0001：统一应用数据库 Schema

- 状态：Accepted
- 日期：2026-09-15

## 背景

历史部署把 OpenFarm 表放在 `public`，把农业遥感表放在 `agri`。这种拆分让 ORM、原生 SQL、导入脚本和运维检查必须同时维护两套命名空间，也容易在新代码中误写到默认的 `public`。

## 决策

应用业务表统一放在 PostgreSQL schema `agric_satellite`，包括原 OpenFarm 表和原 `agri` 表/视图。所有 API、ingest、MQ writer、Alembic 和维护脚本都使用这个 schema；数据库连接统一设置 `search_path=agric_satellite`，ORM metadata 显式声明 `schema="agric_satellite"`。

`agric_satellite` 同时承载应用表、PostGIS、`uuid-ossp`、`fuzzystrmatch` 和 Alembic 版本表。迁移通过 `ALTER ... SET SCHEMA` 移动现有对象，不复制数据；完成迁移后删除 `public` schema。内置 `plpgsql` 属于 PostgreSQL 的 `pg_catalog`，不是可迁移的用户扩展，不会制造 `public` 对象。

## 影响

- 业务查询不再依赖已删除的 `agri` schema。
- 新数据库从首个 Alembic migration 开始就创建业务表到 `agric_satellite`。
- PostGIS 系统对象与 `fields.geom` 等空间字段继续可用，但命名空间统一在 `agric_satellite`。
- 运行旧版 SQL 脚本前需要更新到仓库当前版本；当前脚本已统一指向 `agric_satellite`。
- `0023_unify_application_schema` 负责业务对象搬迁，`0024_remove_public_schema` 负责扩展/版本表搬迁和删除 `public`；两步均在事务中执行。

## 未采用的方案

- 不把 `public` 改名：PostgreSQL 会在新数据库中创建它，且 PostGIS 旧版本的默认安装位置是它；迁移完成后直接删除更容易验证最终状态。
- 不继续保留 `public` + `agri` 双业务 schema：这会继续产生跨服务搜索路径和脚本引用错误。
- 不使用带连字符的 `agric-satellite`：未加引号的 SQL 标识符不允许连字符，且会增加 ORM/迁移复杂度。
