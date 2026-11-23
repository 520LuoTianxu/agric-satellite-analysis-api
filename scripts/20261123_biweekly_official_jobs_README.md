# 官方双周报 official jobs 表迁移（供 DMS 手工执行）

目标库：生产 PostgreSQL 的 `agric_satellite` schema（与现有 `jobs` 表同一数据库）。
本目录文件由隔离真实 PostgreSQL 16.16 容器（本机 127.0.0.1:55432，独立网络/数据卷，未使用业务 compose）验证：迁移脚本连续执行两次均成功，结构冲突与权限不足会显式失败。

**不要**由机器人在生产执行。由有权人员在 DMS 上自行执行。

## 文件

| 文件 | 用途 |
| --- | --- |
| `20261123_biweekly_official_jobs.sql` | 正式 DDL。同一事务整段执行。 |
| `20261123_biweekly_official_jobs_precheck.sql` | 只读执行前检查。 |
| `20261123_biweekly_official_jobs_postcheck.sql` | 只读执行后验收。 |

脚本不修改、不删除、不回填既有表数据；不创建 schema；不写入签名 URL；不含口令或个人数据。历史 `/tmp` 或 work_dir 任务不会被迁入此表。

## 建议执行顺序

1. 只读跑 `precheck.sql`。确认：PostgreSQL ≥ 12（生产基线按 16）；存在 `agric_satellite` 与 `agric_satellite.jobs`，且 `jobs.id` 为 `uuid`；当前角色对 schema 有 `CREATE`，对 `jobs` 有 `REFERENCES`（或为所有者）；记录已在 `jobs` 上拥有 DML 的角色，供事后对照 API 账号。若 `biweekly_official_jobs` 已存在，先看列/约束是否已与脚本一致。
2. 低峰窗口，将 `20261123_biweekly_official_jobs.sql` **作为同一脚本整段执行**，不要逐条自动提交。
3. 只读跑 `postcheck.sql`。确认表、主键、外键、四个 CHECK、两个索引，以及新会话中 `lock_timeout`/`statement_timeout` 仍为库默认值。新表行数应为 0。`jobs` 列不应被本迁移改变。
4. 用 precheck 中对照到的 API 业务账号，由有权角色**另行**授予 DML（本迁移文件故意不含 GRANT，避免写错账号）：
   `GRANT SELECT, INSERT, UPDATE, DELETE ON agric_satellite.biweekly_official_jobs TO <api_role>;`
5. 再部署包含此表读写的 API，然后部署新下载机。滚动期旧下载机仍可能发送 `content_base64`。不要先删旧协议。
6. 若 API 先于本表部署：缺表是显式“未迁移”信号，不得把其它数据库错误当成“没有任务”。

## 幂等与失败

- 第一次：创建表和索引。
- 第二次（结构一致）：成功结束，数据不变。
- 表已存在但结构不符：脚本 `RAISE` 并回滚，不会自动 `ALTER`。
- 权限不足、缺 schema、缺 `jobs`、或 `jobs.id` 不是 uuid：显式失败并回滚。

## 锁与超时

事务内 `SET LOCAL lock_timeout = '15s'`、`SET LOCAL statement_timeout = '60s'`，只作用于本事务。
新建空表时，外键会短暂对 `agric_satellite.jobs` 加 `SHARE ROW EXCLUSIVE`，不改 `jobs` 行；新表为空，校验很快。若 15 秒内拿不到锁，脚本失败回滚，避免长时间堵住业务。

## 与代码的对应

SQLAlchemy 模型 `BiweeklyOfficialJob` 映射本表。主键/列/外键名/索引名与脚本一致：

- `ix_biweekly_official_jobs_period` (`land_id, season_id, period_id, created_at`)
- `ix_biweekly_official_jobs_report_job` (`report_job_id`) `WHERE report_job_id IS NOT NULL`
- `biweekly_official_jobs_report_job_id_fkey` → `jobs(id)` `ON DELETE SET NULL`

`jobs.id` 是 UUID，官方任务 id 是 `bj-` + 16 位小写十六进制，因此不能复用 `jobs` 主键。

## 隔离真实 PostgreSQL 验证（非生产）

验证时间：2026-11-23 16:36 Asia/Shanghai  
环境：独立容器 `t9-oss-official-jobs-pg`，镜像 `postgres:16`（16.16），仅监听 `127.0.0.1:55432`，独立 Docker 网络与数据卷；未使用、未修改业务 compose 容器或 `agric-satellite-analysis-api_default` 网络。

在仅有 `agric_satellite` schema 与最小 `jobs(id uuid)` 的夹具库上：

- 执行前 precheck：表尚不存在，schema/`jobs.id`/REFERENCES 正常。
- 第一次执行脚本：创建表、主键、外键、四个 CHECK、两个索引；事务提交后新会话 `lock_timeout`/`statement_timeout` 仍为 `0`。
- 插入一行官方任务后第二次执行：提示结构已一致、索引已存在则跳过；该行仍在，`jobs` 列未变。
- `artifact_json` 含 `signed_url` 的插入被 `biweekly_official_jobs_artifact_no_url_ck` 拒绝。
- 负例均显式失败且不改错误对象：缺 schema、缺 `jobs`、已有同名但结构不符的表（保持原两列，未自动 ALTER）、无所有权角色在 `CREATE INDEX` 时报 `must be owner`。

此验证不能代替生产 DMS 执行前的 precheck。生产 API 账号名未写进脚本。
