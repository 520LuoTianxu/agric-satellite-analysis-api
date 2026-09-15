# ADR-0001：统一应用数据库 Schema

- 状态：Accepted
- 日期：2026-09-15

## 背景

历史部署把 OpenFarm 表放在 `public`，把农业遥感表放在 `agri`。这种拆分让 ORM、原生 SQL、导入脚本和运维检查必须同时维护两套命名空间，也容易在新代码中误写到默认的 `public`。

## 决策

应用业务表统一放在 PostgreSQL schema `agric_satellite`，包括原 OpenFarm 表和原 `agri` 表/视图。所有 API、ingest、MQ writer、Alembic 和维护脚本都使用这个 schema；数据库连接统一设置 `search_path=agric_satellite,public`，ORM metadata 显式声明 `schema="agric_satellite"`。

`public` 不作为业务 schema 使用，只保留 PostGIS 的 `spatial_ref_sys`、空间元数据视图、扩展对象以及 `public.alembic_version`。迁移通过 `ALTER ... SET SCHEMA` 移动现有对象，不复制数据。

## 影响

- 业务查询不再依赖已删除的 `agri` schema。
- 新数据库从首个 Alembic migration 开始就创建业务表到 `agric_satellite`。
- `public` 中的 PostGIS 系统对象继续可用，`fields.geom` 等空间字段不受影响。
- 运行旧版 SQL 脚本前需要更新到仓库当前版本；当前脚本已统一指向 `agric_satellite`。
- `0023_unify_application_schema` 在迁移前检查目标冲突，并支持回滚到原 `public`/`agri` 布局。

## 未采用的方案

- 不重命名 `public`：它是 PostgreSQL 默认 schema，且 PostGIS 系统对象依赖它，重命名会增加扩展和客户端兼容风险。
- 不继续保留 `public` + `agri` 双业务 schema：这会继续产生跨服务搜索路径和脚本引用错误。
- 不使用带连字符的 `agric-satellite`：未加引号的 SQL 标识符不允许连字符，且会增加 ORM/迁移复杂度。
