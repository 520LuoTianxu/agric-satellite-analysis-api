-- agric_satellite.biweekly_official_jobs
-- 只向前新增官方双周报任务表与索引，不修改、不删除、不回填既有表数据。
-- 本文件不会在 API 启动时自动执行，须由有权角色在受控窗口手工执行。
--
-- 兼容：以项目 docker-compose 的 PostgreSQL 16 为基线；语法仅使用
-- PostgreSQL 12+ 即有的功能（SET LOCAL、jsonb ?、CREATE INDEX IF NOT EXISTS、
-- 命名 CHECK / FOREIGN KEY）。请勿在低于 12 的实例上执行。
--
-- 事务：请将本文件作为同一脚本整段执行，不要逐条自动提交。
-- BEGIN 之后的 SET LOCAL 只作用于本事务，提交或回滚后不会改会话/库级超时。
--
-- 锁：新建空表时，FOREIGN KEY 会短暂对 agric_satellite.jobs 加
-- SHARE ROW EXCLUSIVE（不改 jobs 行）。新表为空，外键校验几乎立即完成。
-- 若 15 秒内无法获得该锁，本脚本显式失败并回滚，而不会长时间堵住业务。
--
-- 幂等：表不存在则创建；表已存在且列/约束/索引与本文一致则成功结束；
-- 表已存在但结构不符则 RAISE 并回滚，不会自动 ALTER、不会 CREATE TABLE IF NOT
-- EXISTS 后静默跳过。权限不足时由 PostgreSQL 直接报错并回滚。
--
-- 本脚本不写入签名 URL，不包含任何账号口令，也不把历史 /tmp 任务迁入此表。
-- 授权：不猜测 API 账号名，不在此文件中执行 GRANT。见配套 precheck / postcheck。

BEGIN;

SET LOCAL lock_timeout = '15s';
SET LOCAL statement_timeout = '60s';

DO $mig$
DECLARE
    mismatch text[] := ARRAY[]::text[];
    def_kind text;
    idx_def text;
    fk_def text;
    ck_name text;
    jobs_id_type text;
BEGIN
    IF current_setting('server_version_num')::int < 120000 THEN
        RAISE EXCEPTION
            'biweekly_official_jobs requires PostgreSQL 12 or newer; got %',
            current_setting('server_version');
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_namespace WHERE nspname = 'agric_satellite'
    ) THEN
        RAISE EXCEPTION
            'schema agric_satellite does not exist; refusing to create it here. Check you are connected to the correct database.';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'agric_satellite'
          AND c.relname = 'jobs'
          AND c.relkind = 'r'
    ) THEN
        RAISE EXCEPTION
            'table agric_satellite.jobs does not exist; the new official job table cannot reference report_job_id.';
    END IF;

    SELECT pg_catalog.format_type(a.atttypid, a.atttypmod)
      INTO jobs_id_type
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'agric_satellite'
      AND c.relname = 'jobs'
      AND a.attname = 'id'
      AND a.attnum > 0
      AND NOT a.attisdropped;

    IF jobs_id_type IS DISTINCT FROM 'uuid' THEN
        RAISE EXCEPTION
            'agric_satellite.jobs.id type is %, expected uuid; refusing to add a foreign key',
            coalesce(jobs_id_type, '<missing>');
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'agric_satellite'
          AND c.relname = 'biweekly_official_jobs'
          AND c.relkind = 'r'
    ) THEN
        CREATE TABLE agric_satellite.biweekly_official_jobs (
            job_id text NOT NULL,
            land_id text NOT NULL,
            season_id text NOT NULL,
            period_id text NOT NULL,
            kind text NOT NULL DEFAULT 'generate',
            status text NOT NULL,
            report_job_id uuid NULL,
            artifact_json jsonb,
            job_json jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT biweekly_official_jobs_pkey PRIMARY KEY (job_id),
            CONSTRAINT biweekly_official_jobs_report_job_id_fkey
                FOREIGN KEY (report_job_id)
                REFERENCES agric_satellite.jobs(id)
                ON DELETE SET NULL,
            CONSTRAINT biweekly_official_jobs_job_id_ck
                CHECK (job_id ~ '^bj-[a-f0-9]{16}$'),
            CONSTRAINT biweekly_official_jobs_status_ck
                CHECK (status IN (
                    'queued',
                    'running',
                    'dispatched',
                    'registration_pending',
                    'ready',
                    'failed',
                    'evidence_limited'
                )),
            CONSTRAINT biweekly_official_jobs_kind_ck
                CHECK (kind IN ('generate', 'backfill', 'backfill_season')),
            CONSTRAINT biweekly_official_jobs_artifact_no_url_ck
                CHECK (
                    artifact_json IS NULL
                    OR (
                        jsonb_typeof(artifact_json) = 'object'
                        AND NOT (artifact_json ? 'url')
                        AND NOT (artifact_json ? 'signed_url')
                        AND NOT (artifact_json ? 'presigned_url')
                        AND NOT (artifact_json ? 'download_url')
                        AND NOT (artifact_json ? 'href')
                    )
                )
        );
        RAISE NOTICE 'created agric_satellite.biweekly_official_jobs';
        RETURN;
    END IF;

    -- Table already exists: require an exact match, then no-op. Do not ALTER.
    IF EXISTS (
        SELECT 1
        FROM (
            SELECT a.attname AS name,
                   pg_catalog.format_type(a.atttypid, a.atttypmod) AS typ,
                   a.attnotnull AS not_null
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'agric_satellite'
              AND c.relname = 'biweekly_official_jobs'
              AND a.attnum > 0
              AND NOT a.attisdropped
        ) live
        FULL OUTER JOIN (
            VALUES
                ('job_id', 'text', true),
                ('land_id', 'text', true),
                ('season_id', 'text', true),
                ('period_id', 'text', true),
                ('kind', 'text', true),
                ('status', 'text', true),
                ('report_job_id', 'uuid', false),
                ('artifact_json', 'jsonb', false),
                ('job_json', 'jsonb', true),
                ('created_at', 'timestamp with time zone', true),
                ('updated_at', 'timestamp with time zone', true)
        ) AS expected(name, typ, not_null)
          ON live.name = expected.name
        WHERE live.name IS NULL
           OR expected.name IS NULL
           OR live.typ IS DISTINCT FROM expected.typ
           OR live.not_null IS DISTINCT FROM expected.not_null
    ) THEN
        mismatch := mismatch || ARRAY['column types or nullability do not match the expected official job table'];
    END IF;

    SELECT pg_get_expr(ad.adbin, ad.adrelid)
      INTO def_kind
    FROM pg_attrdef ad
    JOIN pg_attribute a ON a.attrelid = ad.adrelid AND a.attnum = ad.adnum
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'agric_satellite'
      AND c.relname = 'biweekly_official_jobs'
      AND a.attname = 'kind';

    IF coalesce(def_kind, '') NOT LIKE '%generate%' THEN
        mismatch := mismatch || ARRAY[format('kind default is %s, expected generate', coalesce(def_kind, '<none>'))];
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'biweekly_official_jobs_pkey'
          AND contype = 'p'
          AND conrelid = 'agric_satellite.biweekly_official_jobs'::regclass
          AND pg_get_constraintdef(oid) = 'PRIMARY KEY (job_id)'
    ) THEN
        mismatch := mismatch || ARRAY['primary key biweekly_official_jobs_pkey on job_id is missing or different'];
    END IF;

    SELECT pg_get_constraintdef(oid)
      INTO fk_def
    FROM pg_constraint
    WHERE conname = 'biweekly_official_jobs_report_job_id_fkey'
      AND contype = 'f'
      AND conrelid = 'agric_satellite.biweekly_official_jobs'::regclass;

    IF fk_def IS DISTINCT FROM
          'FOREIGN KEY (report_job_id) REFERENCES jobs(id) ON DELETE SET NULL'
       AND fk_def IS DISTINCT FROM
          'FOREIGN KEY (report_job_id) REFERENCES agric_satellite.jobs(id) ON DELETE SET NULL' THEN
        mismatch := mismatch || ARRAY[format('foreign key mismatch: %s', coalesce(fk_def, '<missing>'))];
    END IF;

    FOREACH ck_name IN ARRAY ARRAY[
        'biweekly_official_jobs_job_id_ck',
        'biweekly_official_jobs_status_ck',
        'biweekly_official_jobs_kind_ck',
        'biweekly_official_jobs_artifact_no_url_ck'
    ]
    LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conname = ck_name
              AND contype = 'c'
              AND conrelid = 'agric_satellite.biweekly_official_jobs'::regclass
        ) THEN
            mismatch := mismatch || ARRAY[format('check constraint %s is missing', ck_name)];
        END IF;
    END LOOP;

    -- A same-named index with a different definition is a hard failure.
    -- A missing index is added after this block with CREATE INDEX IF NOT EXISTS.
    SELECT pg_get_indexdef(i.indexrelid)
      INTO idx_def
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'agric_satellite'
      AND c.relname = 'ix_biweekly_official_jobs_period';

    IF idx_def IS NOT NULL
       AND idx_def NOT LIKE '%(land_id, season_id, period_id, created_at)%' THEN
        mismatch := mismatch || ARRAY[format('index ix_biweekly_official_jobs_period has unexpected definition: %s', idx_def)];
    END IF;

    SELECT pg_get_indexdef(i.indexrelid)
      INTO idx_def
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'agric_satellite'
      AND c.relname = 'ix_biweekly_official_jobs_report_job';

    IF idx_def IS NOT NULL
       AND (
            idx_def NOT LIKE '%(report_job_id)%'
            OR idx_def NOT LIKE '%WHERE (report_job_id IS NOT NULL)%'
       ) THEN
        mismatch := mismatch || ARRAY[format('index ix_biweekly_official_jobs_report_job has unexpected definition: %s', idx_def)];
    END IF;

    IF mismatch <> ARRAY[]::text[] THEN
        RAISE EXCEPTION
            'agric_satellite.biweekly_official_jobs already exists but does not match this script; refusing to ALTER automatically. Details: %',
            array_to_string(mismatch, '; ');
    END IF;

    RAISE NOTICE 'agric_satellite.biweekly_official_jobs already matches this script; leaving existing data unchanged';
END
$mig$;

CREATE INDEX IF NOT EXISTS ix_biweekly_official_jobs_period
    ON agric_satellite.biweekly_official_jobs (land_id, season_id, period_id, created_at);

CREATE INDEX IF NOT EXISTS ix_biweekly_official_jobs_report_job
    ON agric_satellite.biweekly_official_jobs (report_job_id)
    WHERE report_job_id IS NOT NULL;

COMMENT ON TABLE agric_satellite.biweekly_official_jobs IS
    'Authoritative official biweekly jobs and OSS object references; not a signed-URL cache and not a backfill of historical work_dir job files.';
COMMENT ON COLUMN agric_satellite.biweekly_official_jobs.job_id IS
    'Official id (bj- + 16 lowercase hex). This is not agric_satellite.jobs.id, which is a UUID.';
COMMENT ON COLUMN agric_satellite.biweekly_official_jobs.artifact_json IS
    'Permanent OSS object reference (bucket, key, size_bytes, sha256, content-type). Never a presigned URL.';
COMMENT ON COLUMN agric_satellite.biweekly_official_jobs.job_json IS
    'Full official job document used by GET job / periods / download.';
COMMENT ON COLUMN agric_satellite.biweekly_official_jobs.report_job_id IS
    'Optional link to the business Job UUID in agric_satellite.jobs.';

COMMIT;
