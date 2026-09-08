-- Agri schema bootstrap (DDL only, no COPY data)
-- Extracted from Aliyun agri_export dump (schema agri).
-- Apply: psql -v ON_ERROR_STOP=1 -f scripts/agri_seed/001_agri_schema.sql
-- Or: make agri-schema
--
-- Full seed data: place joined dump at data/agri_export.sql (gitignored). See README.md.
-- Sensor CHECK: S1 | S2 on parcel_scene_products.
-- OSS prefix for pixel JSON: s1s2_parcel/json/

--
-- PostgreSQL database dump
--

-- Dumped from database version 10.23
-- Dumped by pg_dump version 14.24 (Ubuntu 14.24-0ubuntu0.22.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: agri; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA IF NOT EXISTS agri;

SET default_tablespace = '';

--
-- Name: ingest_batch_stats; Type: TABLE; Schema: agri; Owner: -
--

CREATE TABLE agri.ingest_batch_stats (
    batch_stat_id bigint NOT NULL,
    run_id bigint NOT NULL,
    batch_no integer NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone NOT NULL,
    key_source_seconds double precision DEFAULT 0 NOT NULL,
    ledger_query_seconds double precision DEFAULT 0 NOT NULL,
    ledger_commit_seconds double precision DEFAULT 0 NOT NULL,
    download_wall_seconds double precision DEFAULT 0 NOT NULL,
    download_work_seconds double precision DEFAULT 0 NOT NULL,
    parse_work_seconds double precision DEFAULT 0 NOT NULL,
    db_write_seconds double precision DEFAULT 0 NOT NULL,
    commit_seconds double precision DEFAULT 0 NOT NULL,
    total_seconds double precision DEFAULT 0 NOT NULL,
    listed_n integer DEFAULT 0 NOT NULL,
    skipped_n integer DEFAULT 0 NOT NULL,
    pending_n integer DEFAULT 0 NOT NULL,
    downloaded_n integer DEFAULT 0 NOT NULL,
    download_error_n integer DEFAULT 0 NOT NULL,
    upserted_n integer DEFAULT 0 NOT NULL,
    db_error_n integer DEFAULT 0 NOT NULL,
    error_n integer DEFAULT 0 NOT NULL,
    bytes_downloaded bigint DEFAULT 0 NOT NULL,
    batch_fallback boolean DEFAULT false NOT NULL,
    notes text
);

--
-- Name: TABLE ingest_batch_stats; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON TABLE agri.ingest_batch_stats IS 'OSS 到 PostgreSQL 每个批次的完整链路计时和计数明细';

--
-- Name: COLUMN ingest_batch_stats.batch_stat_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.batch_stat_id IS '批次统计明细唯一标识';

--
-- Name: COLUMN ingest_batch_stats.run_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.run_id IS '所属入库运行批次 ID';

--
-- Name: COLUMN ingest_batch_stats.batch_no; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.batch_no IS '运行内批次序号，从 1 开始';

--
-- Name: COLUMN ingest_batch_stats.started_at; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.started_at IS '批次开始时间，包含读取 key 清单耗时';

--
-- Name: COLUMN ingest_batch_stats.finished_at; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.finished_at IS '批次业务提交完成时间';

--
-- Name: COLUMN ingest_batch_stats.key_source_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.key_source_seconds IS '读取本批 key 清单或 ListObjects 的耗时（秒）';

--
-- Name: COLUMN ingest_batch_stats.ledger_query_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.ledger_query_seconds IS '本批批量查询已入库台账的耗时（秒）';

--
-- Name: COLUMN ingest_batch_stats.ledger_commit_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.ledger_commit_seconds IS '本批释放台账查询事务的耗时（秒）';

--
-- Name: COLUMN ingest_batch_stats.download_wall_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.download_wall_seconds IS '本批并行下载阶段墙钟耗时（秒）';

--
-- Name: COLUMN ingest_batch_stats.download_work_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.download_work_seconds IS '本批所有对象下载耗时之和（秒）';

--
-- Name: COLUMN ingest_batch_stats.parse_work_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.parse_work_seconds IS '本批所有对象 JSON 解析和字段映射耗时之和（秒）';

--
-- Name: COLUMN ingest_batch_stats.db_write_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.db_write_seconds IS '本批主表和成功台账写入耗时（秒）';

--
-- Name: COLUMN ingest_batch_stats.commit_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.commit_seconds IS '本批业务事务最终提交耗时（秒）';

--
-- Name: COLUMN ingest_batch_stats.total_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.total_seconds IS '本批从读取 key 到业务提交完成的端到端耗时（秒）';

--
-- Name: COLUMN ingest_batch_stats.listed_n; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.listed_n IS '本批读取到的 key 数量';

--
-- Name: COLUMN ingest_batch_stats.skipped_n; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.skipped_n IS '本批命中成功台账而跳过的 key 数量';

--
-- Name: COLUMN ingest_batch_stats.pending_n; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.pending_n IS '本批需要下载的 key 数量';

--
-- Name: COLUMN ingest_batch_stats.downloaded_n; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.downloaded_n IS '本批成功下载并解析的对象数量';

--
-- Name: COLUMN ingest_batch_stats.download_error_n; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.download_error_n IS '本批下载或 JSON 解析失败的对象数量';

--
-- Name: COLUMN ingest_batch_stats.upserted_n; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.upserted_n IS '本批成功写入或更新主表的对象数量';

--
-- Name: COLUMN ingest_batch_stats.db_error_n; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.db_error_n IS '本批 PostgreSQL 写入失败的对象数量';

--
-- Name: COLUMN ingest_batch_stats.error_n; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.error_n IS '本批全部错误数量，等于下载解析错误加数据库错误';

--
-- Name: COLUMN ingest_batch_stats.bytes_downloaded; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.bytes_downloaded IS '本批从 OSS 读取的 JSON 字节数';

--
-- Name: COLUMN ingest_batch_stats.batch_fallback; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.batch_fallback IS '批量 SQL 是否失败并回退为逐条写入';

--
-- Name: COLUMN ingest_batch_stats.notes; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_batch_stats.notes IS '批次统计补充说明';

--
-- Name: ingest_batch_stats_batch_stat_id_seq; Type: SEQUENCE; Schema: agri; Owner: -
--

CREATE SEQUENCE agri.ingest_batch_stats_batch_stat_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: ingest_batch_stats_batch_stat_id_seq; Type: SEQUENCE OWNED BY; Schema: agri; Owner: -
--

ALTER SEQUENCE agri.ingest_batch_stats_batch_stat_id_seq OWNED BY agri.ingest_batch_stats.batch_stat_id;

--
-- Name: ingest_runs; Type: TABLE; Schema: agri; Owner: -
--

CREATE TABLE agri.ingest_runs (
    run_id bigint NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    oss_prefix text,
    oss_bucket text,
    limit_n integer,
    listed_n integer,
    upserted_n integer,
    error_n integer,
    dry_run boolean DEFAULT false,
    notes text,
    status text DEFAULT 'running'::text,
    batches_n integer DEFAULT 0 NOT NULL,
    skipped_n integer DEFAULT 0 NOT NULL,
    downloaded_n integer DEFAULT 0 NOT NULL,
    download_error_n integer DEFAULT 0 NOT NULL,
    db_error_n integer DEFAULT 0 NOT NULL,
    bytes_downloaded bigint DEFAULT 0 NOT NULL,
    elapsed_seconds double precision DEFAULT 0 NOT NULL,
    key_source_seconds double precision DEFAULT 0 NOT NULL,
    ledger_query_seconds double precision DEFAULT 0 NOT NULL,
    ledger_commit_seconds double precision DEFAULT 0 NOT NULL,
    download_wall_seconds double precision DEFAULT 0 NOT NULL,
    download_work_seconds double precision DEFAULT 0 NOT NULL,
    parse_work_seconds double precision DEFAULT 0 NOT NULL,
    db_write_seconds double precision DEFAULT 0 NOT NULL,
    commit_seconds double precision DEFAULT 0 NOT NULL
);

--
-- Name: TABLE ingest_runs; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON TABLE agri.ingest_runs IS 'OSS 到 PostgreSQL 的批次运行日志';

--
-- Name: COLUMN ingest_runs.run_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.run_id IS '入库批次唯一标识';

--
-- Name: COLUMN ingest_runs.started_at; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.started_at IS '批次开始时间';

--
-- Name: COLUMN ingest_runs.finished_at; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.finished_at IS '批次结束时间';

--
-- Name: COLUMN ingest_runs.oss_prefix; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.oss_prefix IS '本次批次使用的 OSS 前缀或本地 key 来源标识';

--
-- Name: COLUMN ingest_runs.oss_bucket; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.oss_bucket IS '本次批次使用的 OSS Bucket';

--
-- Name: COLUMN ingest_runs.limit_n; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.limit_n IS '本次运行最多处理的对象数量；空值表示不限制';

--
-- Name: COLUMN ingest_runs.listed_n; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.listed_n IS '本次运行读取到的 key 数量';

--
-- Name: COLUMN ingest_runs.upserted_n; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.upserted_n IS '本次运行成功写入或更新主表的对象数量';

--
-- Name: COLUMN ingest_runs.error_n; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.error_n IS '本次运行下载、解析或入库失败的对象数量';

--
-- Name: COLUMN ingest_runs.dry_run; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.dry_run IS '是否为只读取 key、不写入数据库的试运行';

--
-- Name: COLUMN ingest_runs.notes; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.notes IS '批次补充说明，包括跳过数量或错误摘要';

--
-- Name: COLUMN ingest_runs.status; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.status IS '批次状态：running、ok、ok_with_errors 或 error';

--
-- Name: COLUMN ingest_runs.batches_n; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.batches_n IS '本次运行处理的批次数量';

--
-- Name: COLUMN ingest_runs.skipped_n; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.skipped_n IS '本次运行因成功台账命中而跳过的对象数量';

--
-- Name: COLUMN ingest_runs.downloaded_n; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.downloaded_n IS '本次运行成功下载并解析的对象数量';

--
-- Name: COLUMN ingest_runs.download_error_n; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.download_error_n IS '本次运行下载或 JSON 解析失败的对象数量';

--
-- Name: COLUMN ingest_runs.db_error_n; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.db_error_n IS '本次运行 PostgreSQL 写入失败的对象数量';

--
-- Name: COLUMN ingest_runs.bytes_downloaded; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.bytes_downloaded IS '本次运行从 OSS 读取的 JSON 字节数';

--
-- Name: COLUMN ingest_runs.elapsed_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.elapsed_seconds IS '本次运行端到端墙钟耗时（秒；不含最终运行日志更新）';

--
-- Name: COLUMN ingest_runs.key_source_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.key_source_seconds IS '读取 key 清单或调用 ListObjects 的累计耗时（秒）';

--
-- Name: COLUMN ingest_runs.ledger_query_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.ledger_query_seconds IS '批量查询成功入库台账的累计耗时（秒）';

--
-- Name: COLUMN ingest_runs.ledger_commit_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.ledger_commit_seconds IS '释放台账查询事务的累计提交耗时（秒）';

--
-- Name: COLUMN ingest_runs.download_wall_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.download_wall_seconds IS '并行下载阶段的累计墙钟耗时（秒）';

--
-- Name: COLUMN ingest_runs.download_work_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.download_work_seconds IS '所有对象下载耗时之和（秒；并行时会大于墙钟耗时）';

--
-- Name: COLUMN ingest_runs.parse_work_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.parse_work_seconds IS '所有对象 JSON 解析和字段映射耗时之和（秒）';

--
-- Name: COLUMN ingest_runs.db_write_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.db_write_seconds IS '批量 upsert 主表和写入成功台账的累计耗时（秒）';

--
-- Name: COLUMN ingest_runs.commit_seconds; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingest_runs.commit_seconds IS '业务批次最终提交事务的累计耗时（秒）';

--
-- Name: ingest_runs_run_id_seq; Type: SEQUENCE; Schema: agri; Owner: -
--

CREATE SEQUENCE agri.ingest_runs_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

--
-- Name: ingest_runs_run_id_seq; Type: SEQUENCE OWNED BY; Schema: agri; Owner: -
--

ALTER SEQUENCE agri.ingest_runs_run_id_seq OWNED BY agri.ingest_runs.run_id;

--
-- Name: ingested_oss_objects; Type: TABLE; Schema: agri; Owner: -
--

CREATE TABLE agri.ingested_oss_objects (
    json_oss_key text NOT NULL,
    land_id text,
    tile_id text,
    product_date date,
    sensor text,
    scene_id text,
    ingest_run_id bigint,
    ingested_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: TABLE ingested_oss_objects; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON TABLE agri.ingested_oss_objects IS '已成功写入 PostgreSQL 的 OSS JSON key 台账，用于增量去重';

--
-- Name: COLUMN ingested_oss_objects.json_oss_key; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingested_oss_objects.json_oss_key IS '已成功入库的源 JSON OSS 对象 key；主键用于防止重复处理';

--
-- Name: COLUMN ingested_oss_objects.land_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingested_oss_objects.land_id IS '已入库产品的地块业务标识';

--
-- Name: COLUMN ingested_oss_objects.tile_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingested_oss_objects.tile_id IS '已入库产品的 Sentinel 瓦片标识';

--
-- Name: COLUMN ingested_oss_objects.product_date; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingested_oss_objects.product_date IS '已入库产品日期';

--
-- Name: COLUMN ingested_oss_objects.sensor; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingested_oss_objects.sensor IS '已入库产品的传感器类型';

--
-- Name: COLUMN ingested_oss_objects.scene_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingested_oss_objects.scene_id IS '已入库产品的卫星场景标识';

--
-- Name: COLUMN ingested_oss_objects.ingest_run_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingested_oss_objects.ingest_run_id IS '写入该对象的入库批次 ID';

--
-- Name: COLUMN ingested_oss_objects.ingested_at; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.ingested_oss_objects.ingested_at IS '该 OSS 对象成功写入主表的时间';

--
-- Name: land_parcels; Type: TABLE; Schema: agri; Owner: -
--

CREATE TABLE agri.land_parcels (
    land_id text NOT NULL,
    source_parcel_id text,
    tile_id text NOT NULL,
    virtual_tile_id text,
    project_key text,
    tile_assignment_type text,
    tile_anchor_land_id text,
    land_name text,
    group_id text,
    group_name text,
    org_code text,
    org_name text,
    base_id text,
    province_code text,
    province_name text,
    city_code text,
    city_name text,
    county_code text,
    county_name text,
    town_code text,
    town_name text,
    village_code text,
    village_name text,
    original_area_mu numeric(18,4),
    land_area_mu numeric(18,4),
    soil_property text,
    current_batch text,
    land_status text,
    source_update_time timestamp without time zone,
    boundary_geojson jsonb NOT NULL,
    boundary_srid integer DEFAULT 4326 NOT NULL,
    min_lon double precision NOT NULL,
    min_lat double precision NOT NULL,
    max_lon double precision NOT NULL,
    max_lat double precision NOT NULL,
    source_properties jsonb DEFAULT '{}'::jsonb NOT NULL,
    source_file text NOT NULL,
    source_feature_index integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT land_parcels_boundary_bbox_ck CHECK (((min_lon <= max_lon) AND (min_lat <= max_lat))),
    CONSTRAINT land_parcels_boundary_object_ck CHECK (((jsonb_typeof(boundary_geojson) = 'object'::text) AND ((boundary_geojson ->> 'type'::text) = ANY (ARRAY['Polygon'::text, 'MultiPolygon'::text]))))
);

--
-- Name: TABLE land_parcels; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON TABLE agri.land_parcels IS '地块主数据；保存 land_id、属性、真实边界和源属性快照';

--
-- Name: COLUMN land_parcels.land_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.land_id IS '地块业务主标识；统一使用 land_id，不再以 parcel_id 作为数据库主字段';

--
-- Name: COLUMN land_parcels.source_parcel_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.source_parcel_id IS '源文件中的 parcel_id，保留用于追溯';

--
-- Name: COLUMN land_parcels.tile_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.tile_id IS '地块所属虚拟项目区/处理瓦片标识';

--
-- Name: COLUMN land_parcels.virtual_tile_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.virtual_tile_id IS '源文件中的虚拟瓦片标识';

--
-- Name: COLUMN land_parcels.project_key; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.project_key IS '项目区业务标识';

--
-- Name: COLUMN land_parcels.tile_assignment_type; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.tile_assignment_type IS '地块分配到虚拟项目区的类型';

--
-- Name: COLUMN land_parcels.tile_anchor_land_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.tile_anchor_land_id IS '虚拟项目区锚点地块的 land_id';

--
-- Name: COLUMN land_parcels.land_name; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.land_name IS '地块名称';

--
-- Name: COLUMN land_parcels.group_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.group_id IS '业务分组标识';

--
-- Name: COLUMN land_parcels.group_name; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.group_name IS '业务分组名称';

--
-- Name: COLUMN land_parcels.org_code; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.org_code IS '组织编码';

--
-- Name: COLUMN land_parcels.org_name; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.org_name IS '组织名称';

--
-- Name: COLUMN land_parcels.base_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.base_id IS '基地标识';

--
-- Name: COLUMN land_parcels.province_code; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.province_code IS '省级行政区编码';

--
-- Name: COLUMN land_parcels.province_name; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.province_name IS '省级行政区名称';

--
-- Name: COLUMN land_parcels.city_code; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.city_code IS '市级行政区编码';

--
-- Name: COLUMN land_parcels.city_name; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.city_name IS '市级行政区名称';

--
-- Name: COLUMN land_parcels.county_code; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.county_code IS '县级行政区编码';

--
-- Name: COLUMN land_parcels.county_name; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.county_name IS '县级行政区名称';

--
-- Name: COLUMN land_parcels.town_code; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.town_code IS '乡镇行政区编码';

--
-- Name: COLUMN land_parcels.town_name; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.town_name IS '乡镇行政区名称';

--
-- Name: COLUMN land_parcels.village_code; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.village_code IS '村级行政区编码';

--
-- Name: COLUMN land_parcels.village_name; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.village_name IS '村级行政区名称';

--
-- Name: COLUMN land_parcels.original_area_mu; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.original_area_mu IS '源数据原始面积，单位为亩';

--
-- Name: COLUMN land_parcels.land_area_mu; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.land_area_mu IS '源数据地块面积，单位为亩';

--
-- Name: COLUMN land_parcels.soil_property; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.soil_property IS '土壤属性编码';

--
-- Name: COLUMN land_parcels.current_batch; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.current_batch IS '当前批次标识';

--
-- Name: COLUMN land_parcels.land_status; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.land_status IS '地块状态编码';

--
-- Name: COLUMN land_parcels.source_update_time; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.source_update_time IS '源数据更新时间，按源文件原始本地时间保存';

--
-- Name: COLUMN land_parcels.boundary_geojson; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.boundary_geojson IS '地块真实边界 GeoJSON；WGS84 EPSG:4326；源 Polygon/MultiPolygon 原样保存';

--
-- Name: COLUMN land_parcels.boundary_srid; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.boundary_srid IS '边界坐标参考系 EPSG 编码，当前为 4326';

--
-- Name: COLUMN land_parcels.min_lon; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.min_lon IS '边界经度最小值，用于包围盒粗筛';

--
-- Name: COLUMN land_parcels.min_lat; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.min_lat IS '边界纬度最小值，用于包围盒粗筛';

--
-- Name: COLUMN land_parcels.max_lon; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.max_lon IS '边界经度最大值，用于包围盒粗筛';

--
-- Name: COLUMN land_parcels.max_lat; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.max_lat IS '边界纬度最大值，用于包围盒粗筛';

--
-- Name: COLUMN land_parcels.source_properties; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.source_properties IS '源 GeoJSON properties 的完整快照，防止未标准化字段丢失';

--
-- Name: COLUMN land_parcels.source_file; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.source_file IS '边界来源文件';

--
-- Name: COLUMN land_parcels.source_feature_index; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.source_feature_index IS '来源 GeoJSON 中的 feature 顺序，从 0 开始';

--
-- Name: COLUMN land_parcels.created_at; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.created_at IS '本条地块记录首次写入时间';

--
-- Name: COLUMN land_parcels.updated_at; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.land_parcels.updated_at IS '本条地块记录最近更新时间';

--
-- Name: parcel_scene_products; Type: TABLE; Schema: agri; Owner: -
--

CREATE TABLE agri.parcel_scene_products (
    land_id text NOT NULL,
    tile_id text NOT NULL,
    date date NOT NULL,
    sensor text NOT NULL,
    scene_id text DEFAULT ''::text NOT NULL,
    land_name text,
    cloud_cover double precision,
    cloud_cover_over_30 boolean,
    parcel_cloud_cover_pct double precision,
    json_oss_key text,
    pixel_count integer,
    generated_at_shanghai text,
    ingested_at timestamp with time zone DEFAULT now() NOT NULL,
    pixel_data_url text NOT NULL,
    evi_avg double precision,
    evi_min double precision,
    evi_max double precision,
    ndmi_avg double precision,
    ndmi_min double precision,
    ndmi_max double precision,
    ndre_avg double precision,
    ndre_min double precision,
    ndre_max double precision,
    ndvi_avg double precision,
    ndvi_min double precision,
    ndvi_max double precision,
    mndwi_avg double precision,
    mndwi_min double precision,
    mndwi_max double precision,
    vv_avg double precision,
    vv_min double precision,
    vv_max double precision,
    vh_avg double precision,
    vh_min double precision,
    vh_max double precision,
    pixel_data jsonb NOT NULL,
    cire_avg double precision,
    cire_min double precision,
    cire_max double precision,
    CONSTRAINT parcel_scene_products_pixel_data_object_ck CHECK ((jsonb_typeof(pixel_data) = 'object'::text)),
    CONSTRAINT parcel_scene_products_sensor_ck CHECK ((sensor = ANY (ARRAY['S1'::text, 'S2'::text])))
);

--
-- Name: TABLE parcel_scene_products; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON TABLE agri.parcel_scene_products IS 'Sentinel-1/2 地块场景产品（自 Aliyun OSS JSON 入库）';

--
-- Name: COLUMN parcel_scene_products.land_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.land_id IS '地块业务标识；兼容历史产品 JSON 中的 parcel_id';

--
-- Name: COLUMN parcel_scene_products.tile_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.tile_id IS 'Sentinel 瓦片标识';

--
-- Name: COLUMN parcel_scene_products.date; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.date IS '产品观测日期';

--
-- Name: COLUMN parcel_scene_products.sensor; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.sensor IS '传感器类型：S1 或 S2';

--
-- Name: COLUMN parcel_scene_products.scene_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.scene_id IS '卫星场景标识；同一地块、日期、传感器下用于区分场景';

--
-- Name: COLUMN parcel_scene_products.land_name; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.land_name IS '地块名称';

--
-- Name: COLUMN parcel_scene_products.cloud_cover; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.cloud_cover IS '场景云量；单位和取值范围沿用源产品定义';

--
-- Name: COLUMN parcel_scene_products.cloud_cover_over_30; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.cloud_cover_over_30 IS '场景云量是否超过 30%';

--
-- Name: COLUMN parcel_scene_products.parcel_cloud_cover_pct; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.parcel_cloud_cover_pct IS '地块云量百分比；历史字段名保留以兼容源 JSON';

--
-- Name: COLUMN parcel_scene_products.json_oss_key; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.json_oss_key IS '源 JSON 在 OSS 中的对象 key';

--
-- Name: COLUMN parcel_scene_products.pixel_count; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.pixel_count IS '产品像元总数';

--
-- Name: COLUMN parcel_scene_products.generated_at_shanghai; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.generated_at_shanghai IS '产品生成时间，源数据使用上海时区字符串表示';

--
-- Name: COLUMN parcel_scene_products.ingested_at; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.ingested_at IS '本次写入 PostgreSQL 的时间';

--
-- Name: COLUMN parcel_scene_products.pixel_data_url; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.pixel_data_url IS '原始 JSON 在 OSS 中的稳定 oss:// 地址；不保存短期签名 URL';

--
-- Name: COLUMN parcel_scene_products.evi_avg; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.evi_avg IS 'EVI 像元平均值';

--
-- Name: COLUMN parcel_scene_products.evi_min; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.evi_min IS 'EVI 像元最小值';

--
-- Name: COLUMN parcel_scene_products.evi_max; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.evi_max IS 'EVI 像元最大值';

--
-- Name: COLUMN parcel_scene_products.ndmi_avg; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.ndmi_avg IS 'NDMI 像元平均值';

--
-- Name: COLUMN parcel_scene_products.ndmi_min; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.ndmi_min IS 'NDMI 像元最小值';

--
-- Name: COLUMN parcel_scene_products.ndmi_max; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.ndmi_max IS 'NDMI 像元最大值';

--
-- Name: COLUMN parcel_scene_products.ndre_avg; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.ndre_avg IS 'NDRE 像元平均值';

--
-- Name: COLUMN parcel_scene_products.ndre_min; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.ndre_min IS 'NDRE 像元最小值';

--
-- Name: COLUMN parcel_scene_products.ndre_max; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.ndre_max IS 'NDRE 像元最大值';

--
-- Name: COLUMN parcel_scene_products.ndvi_avg; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.ndvi_avg IS 'NDVI 像元平均值';

--
-- Name: COLUMN parcel_scene_products.ndvi_min; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.ndvi_min IS 'NDVI 像元最小值';

--
-- Name: COLUMN parcel_scene_products.ndvi_max; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.ndvi_max IS 'NDVI 像元最大值';

--
-- Name: COLUMN parcel_scene_products.mndwi_avg; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.mndwi_avg IS 'MNDWI 像元平均值';

--
-- Name: COLUMN parcel_scene_products.mndwi_min; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.mndwi_min IS 'MNDWI 像元最小值';

--
-- Name: COLUMN parcel_scene_products.mndwi_max; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.mndwi_max IS 'MNDWI 像元最大值';

--
-- Name: COLUMN parcel_scene_products.vv_avg; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.vv_avg IS 'S1 VV 像元平均值';

--
-- Name: COLUMN parcel_scene_products.vv_min; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.vv_min IS 'S1 VV 像元最小值';

--
-- Name: COLUMN parcel_scene_products.vv_max; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.vv_max IS 'S1 VV 像元最大值';

--
-- Name: COLUMN parcel_scene_products.vh_avg; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.vh_avg IS 'S1 VH 像元平均值';

--
-- Name: COLUMN parcel_scene_products.vh_min; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.vh_min IS 'S1 VH 像元最小值';

--
-- Name: COLUMN parcel_scene_products.vh_max; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.vh_max IS 'S1 VH 像元最大值';

--
-- Name: COLUMN parcel_scene_products.pixel_data; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.pixel_data IS '紧凑像素 JSONB 副本；结构为 grid、columns、pixels，仅去掉重复经纬度和 clear，保留 CIre 等指标';

--
-- Name: COLUMN parcel_scene_products.cire_avg; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.cire_avg IS 'CIre 像元平均值';

--
-- Name: COLUMN parcel_scene_products.cire_min; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.cire_min IS 'CIre 像元最小值';

--
-- Name: COLUMN parcel_scene_products.cire_max; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.parcel_scene_products.cire_max IS 'CIre 像元最大值';

--
-- Name: v_land_parcels_detail; Type: VIEW; Schema: agri; Owner: -
--

CREATE VIEW agri.v_land_parcels_detail AS
 SELECT p.land_id,
    p.source_parcel_id,
    p.tile_id,
    p.virtual_tile_id,
    p.project_key,
    p.tile_assignment_type,
    p.tile_anchor_land_id,
    p.land_name,
    p.group_id,
    p.group_name,
    p.org_code,
    p.org_name,
    p.base_id,
    p.province_code,
    p.province_name,
    p.city_code,
    p.city_name,
    p.county_code,
    p.county_name,
    p.town_code,
    p.town_name,
    p.village_code,
    p.village_name,
    p.original_area_mu,
    p.land_area_mu,
    p.soil_property,
    p.current_batch,
    p.land_status,
    p.source_update_time,
    p.boundary_geojson,
    p.boundary_srid,
    p.min_lon,
    p.min_lat,
    p.max_lon,
    p.max_lat,
    p.source_properties,
    p.source_file,
    p.source_feature_index,
    p.created_at,
    p.updated_at
   FROM agri.land_parcels p;

--
-- Name: VIEW v_land_parcels_detail; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON VIEW agri.v_land_parcels_detail IS '地块详情视图；通过 boundary_geojson 直接返回完整 GeoJSON 边界';

--
-- Name: v_parcel_scene_products_meta; Type: VIEW; Schema: agri; Owner: -
--

CREATE VIEW agri.v_parcel_scene_products_meta AS
 SELECT parcel_scene_products.land_id,
    parcel_scene_products.tile_id,
    parcel_scene_products.date,
    parcel_scene_products.sensor,
    parcel_scene_products.scene_id,
    parcel_scene_products.land_name,
    parcel_scene_products.cloud_cover,
    parcel_scene_products.cloud_cover_over_30,
    parcel_scene_products.parcel_cloud_cover_pct,
    parcel_scene_products.json_oss_key,
    parcel_scene_products.pixel_data_url,
    parcel_scene_products.pixel_count,
    parcel_scene_products.evi_avg,
    parcel_scene_products.evi_min,
    parcel_scene_products.evi_max,
    parcel_scene_products.cire_avg,
    parcel_scene_products.cire_min,
    parcel_scene_products.cire_max,
    parcel_scene_products.ndmi_avg,
    parcel_scene_products.ndmi_min,
    parcel_scene_products.ndmi_max,
    parcel_scene_products.ndre_avg,
    parcel_scene_products.ndre_min,
    parcel_scene_products.ndre_max,
    parcel_scene_products.ndvi_avg,
    parcel_scene_products.ndvi_min,
    parcel_scene_products.ndvi_max,
    parcel_scene_products.mndwi_avg,
    parcel_scene_products.mndwi_min,
    parcel_scene_products.mndwi_max,
    parcel_scene_products.vv_avg,
    parcel_scene_products.vv_min,
    parcel_scene_products.vv_max,
    parcel_scene_products.vh_avg,
    parcel_scene_products.vh_min,
    parcel_scene_products.vh_max,
    parcel_scene_products.generated_at_shanghai,
    parcel_scene_products.ingested_at
   FROM agri.parcel_scene_products;

--
-- Name: virtual_project_areas; Type: TABLE; Schema: agri; Owner: -
--

CREATE TABLE agri.virtual_project_areas (
    tile_id text NOT NULL,
    project_key text,
    anchor_land_id text,
    assignment_type text,
    parcel_count integer,
    tile_width_m double precision,
    tile_height_m double precision,
    group_id text,
    group_name text,
    base_id text,
    org_code text,
    org_name text,
    province_name text,
    city_name text,
    county_name text,
    boundary_geojson jsonb NOT NULL,
    boundary_srid integer DEFAULT 4326 NOT NULL,
    min_lon double precision NOT NULL,
    min_lat double precision NOT NULL,
    max_lon double precision NOT NULL,
    max_lat double precision NOT NULL,
    source_properties jsonb DEFAULT '{}'::jsonb NOT NULL,
    source_file text NOT NULL,
    source_feature_index integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT virtual_project_areas_boundary_bbox_ck CHECK (((min_lon <= max_lon) AND (min_lat <= max_lat))),
    CONSTRAINT virtual_project_areas_boundary_object_ck CHECK (((jsonb_typeof(boundary_geojson) = 'object'::text) AND ((boundary_geojson ->> 'type'::text) = ANY (ARRAY['Polygon'::text, 'MultiPolygon'::text]))))
);

--
-- Name: TABLE virtual_project_areas; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON TABLE agri.virtual_project_areas IS '虚拟项目区/5×5 km 瓦片主数据及其边界';

--
-- Name: COLUMN virtual_project_areas.tile_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.tile_id IS '虚拟项目区/5×5 km 瓦片标识';

--
-- Name: COLUMN virtual_project_areas.project_key; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.project_key IS '项目业务标识';

--
-- Name: COLUMN virtual_project_areas.anchor_land_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.anchor_land_id IS '锚点地块的 land_id';

--
-- Name: COLUMN virtual_project_areas.assignment_type; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.assignment_type IS '虚拟项目区分配类型';

--
-- Name: COLUMN virtual_project_areas.parcel_count; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.parcel_count IS '项目区内地块数量';

--
-- Name: COLUMN virtual_project_areas.tile_width_m; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.tile_width_m IS '项目区宽度，单位为米';

--
-- Name: COLUMN virtual_project_areas.tile_height_m; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.tile_height_m IS '项目区高度，单位为米';

--
-- Name: COLUMN virtual_project_areas.group_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.group_id IS '业务分组标识';

--
-- Name: COLUMN virtual_project_areas.group_name; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.group_name IS '业务分组名称';

--
-- Name: COLUMN virtual_project_areas.base_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.base_id IS '基地标识';

--
-- Name: COLUMN virtual_project_areas.org_code; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.org_code IS '组织编码';

--
-- Name: COLUMN virtual_project_areas.org_name; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.org_name IS '组织名称';

--
-- Name: COLUMN virtual_project_areas.province_name; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.province_name IS '省级行政区名称';

--
-- Name: COLUMN virtual_project_areas.city_name; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.city_name IS '市级行政区名称';

--
-- Name: COLUMN virtual_project_areas.county_name; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.county_name IS '县级行政区名称';

--
-- Name: COLUMN virtual_project_areas.boundary_geojson; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.boundary_geojson IS '虚拟项目区边界 GeoJSON；WGS84 EPSG:4326；源 Polygon/MultiPolygon 原样保存';

--
-- Name: COLUMN virtual_project_areas.boundary_srid; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.boundary_srid IS '边界坐标参考系 EPSG 编码，当前为 4326';

--
-- Name: COLUMN virtual_project_areas.min_lon; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.min_lon IS '边界经度最小值，用于包围盒粗筛';

--
-- Name: COLUMN virtual_project_areas.min_lat; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.min_lat IS '边界纬度最小值，用于包围盒粗筛';

--
-- Name: COLUMN virtual_project_areas.max_lon; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.max_lon IS '边界经度最大值，用于包围盒粗筛';

--
-- Name: COLUMN virtual_project_areas.max_lat; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.max_lat IS '边界纬度最大值，用于包围盒粗筛';

--
-- Name: COLUMN virtual_project_areas.source_properties; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.source_properties IS '源 GeoJSON properties 的完整快照';

--
-- Name: COLUMN virtual_project_areas.source_file; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.source_file IS '虚拟项目区边界来源文件';

--
-- Name: COLUMN virtual_project_areas.source_feature_index; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.source_feature_index IS '来源 GeoJSON 中的 feature 顺序，从 0 开始';

--
-- Name: COLUMN virtual_project_areas.created_at; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.created_at IS '本条虚拟项目区记录首次写入时间';

--
-- Name: COLUMN virtual_project_areas.updated_at; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_areas.updated_at IS '本条虚拟项目区记录最近更新时间';

--
-- Name: v_virtual_project_areas_detail; Type: VIEW; Schema: agri; Owner: -
--

CREATE VIEW agri.v_virtual_project_areas_detail AS
 SELECT a.tile_id,
    a.project_key,
    a.anchor_land_id,
    a.assignment_type,
    a.parcel_count,
    a.tile_width_m,
    a.tile_height_m,
    a.group_id,
    a.group_name,
    a.base_id,
    a.org_code,
    a.org_name,
    a.province_name,
    a.city_name,
    a.county_name,
    a.boundary_geojson,
    a.boundary_srid,
    a.min_lon,
    a.min_lat,
    a.max_lon,
    a.max_lat,
    a.source_properties,
    a.source_file,
    a.source_feature_index,
    a.created_at,
    a.updated_at
   FROM agri.virtual_project_areas a;

--
-- Name: VIEW v_virtual_project_areas_detail; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON VIEW agri.v_virtual_project_areas_detail IS '虚拟项目区详情视图；通过 boundary_geojson 直接返回完整 GeoJSON 边界';

--
-- Name: virtual_project_area_lands; Type: TABLE; Schema: agri; Owner: -
--

CREATE TABLE agri.virtual_project_area_lands (
    tile_id text NOT NULL,
    land_id text NOT NULL,
    assignment_type text,
    is_anchor boolean DEFAULT false NOT NULL,
    intersection_area_m2 double precision,
    coverage_ratio double precision,
    source text DEFAULT 'parcels_geoms.geojson'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT virtual_project_area_lands_ratio_ck CHECK (((coverage_ratio IS NULL) OR ((coverage_ratio >= (0)::double precision) AND (coverage_ratio <= (1.000001)::double precision))))
);

--
-- Name: TABLE virtual_project_area_lands; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON TABLE agri.virtual_project_area_lands IS '虚拟项目区与地块的空间归属关系';

--
-- Name: COLUMN virtual_project_area_lands.tile_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_area_lands.tile_id IS '虚拟项目区/瓦片标识';

--
-- Name: COLUMN virtual_project_area_lands.land_id; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_area_lands.land_id IS '地块业务主标识';

--
-- Name: COLUMN virtual_project_area_lands.assignment_type; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_area_lands.assignment_type IS '地块在项目区中的源分配类型';

--
-- Name: COLUMN virtual_project_area_lands.is_anchor; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_area_lands.is_anchor IS '是否为项目区锚点地块';

--
-- Name: COLUMN virtual_project_area_lands.intersection_area_m2; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_area_lands.intersection_area_m2 IS '地块与项目区相交面积，单位为平方米';

--
-- Name: COLUMN virtual_project_area_lands.coverage_ratio; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_area_lands.coverage_ratio IS '项目区覆盖地块的比例';

--
-- Name: COLUMN virtual_project_area_lands.source; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_area_lands.source IS '关系来源文件或来源系统';

--
-- Name: COLUMN virtual_project_area_lands.created_at; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_area_lands.created_at IS '本条项目区-地块关系首次写入时间';

--
-- Name: COLUMN virtual_project_area_lands.updated_at; Type: COMMENT; Schema: agri; Owner: -
--

COMMENT ON COLUMN agri.virtual_project_area_lands.updated_at IS '本条项目区-地块关系最近更新时间';

--
-- Name: ingest_batch_stats batch_stat_id; Type: DEFAULT; Schema: agri; Owner: -
--

ALTER TABLE ONLY agri.ingest_batch_stats ALTER COLUMN batch_stat_id SET DEFAULT nextval('agri.ingest_batch_stats_batch_stat_id_seq'::regclass);

--
-- Name: ingest_runs run_id; Type: DEFAULT; Schema: agri; Owner: -
--

ALTER TABLE ONLY agri.ingest_runs ALTER COLUMN run_id SET DEFAULT nextval('agri.ingest_runs_run_id_seq'::regclass);

--
-- PostgreSQL database dump complete
--

--
-- PostgreSQL database dump
--

-- Dumped from database version 10.23
-- Dumped by pg_dump version 14.24 (Ubuntu 14.24-0ubuntu0.22.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--

--

--

--

--

--

--
-- Name: ingest_batch_stats_batch_stat_id_seq; Type: SEQUENCE SET; Schema: agri; Owner: -
--

SELECT pg_catalog.setval('agri.ingest_batch_stats_batch_stat_id_seq', 84911, true);

--
-- Name: ingest_runs_run_id_seq; Type: SEQUENCE SET; Schema: agri; Owner: -
--

SELECT pg_catalog.setval('agri.ingest_runs_run_id_seq', 18, true);

--
-- PostgreSQL database dump complete
--

-- 遥感产品样例：按 (land_id, date, sensor, scene_id) 稳定排序取前 1000 条。
--
-- PostgreSQL database dump
--

-- Dumped from database version 10.23
-- Dumped by pg_dump version 14.24 (Ubuntu 14.24-0ubuntu0.22.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

--
-- Name: ingest_batch_stats ingest_batch_stats_pkey; Type: CONSTRAINT; Schema: agri; Owner: -
--

ALTER TABLE ONLY agri.ingest_batch_stats
    ADD CONSTRAINT ingest_batch_stats_pkey PRIMARY KEY (batch_stat_id);

--
-- Name: ingest_batch_stats ingest_batch_stats_run_id_batch_no_key; Type: CONSTRAINT; Schema: agri; Owner: -
--

ALTER TABLE ONLY agri.ingest_batch_stats
    ADD CONSTRAINT ingest_batch_stats_run_id_batch_no_key UNIQUE (run_id, batch_no);

--
-- Name: ingest_runs ingest_runs_pkey; Type: CONSTRAINT; Schema: agri; Owner: -
--

ALTER TABLE ONLY agri.ingest_runs
    ADD CONSTRAINT ingest_runs_pkey PRIMARY KEY (run_id);

--
-- Name: ingested_oss_objects ingested_oss_objects_pkey; Type: CONSTRAINT; Schema: agri; Owner: -
--

ALTER TABLE ONLY agri.ingested_oss_objects
    ADD CONSTRAINT ingested_oss_objects_pkey PRIMARY KEY (json_oss_key);

--
-- Name: land_parcels land_parcels_pkey; Type: CONSTRAINT; Schema: agri; Owner: -
--

ALTER TABLE ONLY agri.land_parcels
    ADD CONSTRAINT land_parcels_pkey PRIMARY KEY (land_id);

--
-- Name: parcel_scene_products parcel_scene_products_pkey; Type: CONSTRAINT; Schema: agri; Owner: -
--

ALTER TABLE ONLY agri.parcel_scene_products
    ADD CONSTRAINT parcel_scene_products_pkey PRIMARY KEY (land_id, date, sensor, scene_id);

--
-- Name: virtual_project_area_lands virtual_project_area_lands_pkey; Type: CONSTRAINT; Schema: agri; Owner: -
--

ALTER TABLE ONLY agri.virtual_project_area_lands
    ADD CONSTRAINT virtual_project_area_lands_pkey PRIMARY KEY (tile_id, land_id);

--
-- Name: virtual_project_areas virtual_project_areas_pkey; Type: CONSTRAINT; Schema: agri; Owner: -
--

ALTER TABLE ONLY agri.virtual_project_areas
    ADD CONSTRAINT virtual_project_areas_pkey PRIMARY KEY (tile_id);

--
-- Name: idx_ingest_batch_stats_run; Type: INDEX; Schema: agri; Owner: -
--

CREATE INDEX idx_ingest_batch_stats_run ON agri.ingest_batch_stats USING btree (run_id, batch_no);

--
-- Name: idx_ingested_oss_objects_product; Type: INDEX; Schema: agri; Owner: -
--

CREATE INDEX idx_ingested_oss_objects_product ON agri.ingested_oss_objects USING btree (land_id, product_date, sensor);

--
-- Name: idx_ingested_oss_objects_run; Type: INDEX; Schema: agri; Owner: -
--

CREATE INDEX idx_ingested_oss_objects_run ON agri.ingested_oss_objects USING btree (ingest_run_id);

--
-- Name: idx_psp_date; Type: INDEX; Schema: agri; Owner: -
--

CREATE INDEX idx_psp_date ON agri.parcel_scene_products USING btree (date);

--
-- Name: idx_psp_sensor; Type: INDEX; Schema: agri; Owner: -
--

CREATE INDEX idx_psp_sensor ON agri.parcel_scene_products USING btree (sensor);

--
-- Name: idx_psp_tile_date; Type: INDEX; Schema: agri; Owner: -
--

CREATE INDEX idx_psp_tile_date ON agri.parcel_scene_products USING btree (tile_id, date);

--
-- Name: land_parcels_bbox_idx; Type: INDEX; Schema: agri; Owner: -
--

CREATE INDEX land_parcels_bbox_idx ON agri.land_parcels USING btree (min_lon, max_lon, min_lat, max_lat);

--
-- Name: land_parcels_group_id_idx; Type: INDEX; Schema: agri; Owner: -
--

CREATE INDEX land_parcels_group_id_idx ON agri.land_parcels USING btree (group_id);

--
-- Name: land_parcels_project_key_idx; Type: INDEX; Schema: agri; Owner: -
--

CREATE INDEX land_parcels_project_key_idx ON agri.land_parcels USING btree (project_key);

--
-- Name: land_parcels_tile_id_idx; Type: INDEX; Schema: agri; Owner: -
--

CREATE INDEX land_parcels_tile_id_idx ON agri.land_parcels USING btree (tile_id);

--
-- Name: virtual_project_area_lands_land_id_idx; Type: INDEX; Schema: agri; Owner: -
--

CREATE INDEX virtual_project_area_lands_land_id_idx ON agri.virtual_project_area_lands USING btree (land_id);

--
-- Name: virtual_project_areas_bbox_idx; Type: INDEX; Schema: agri; Owner: -
--

CREATE INDEX virtual_project_areas_bbox_idx ON agri.virtual_project_areas USING btree (min_lon, max_lon, min_lat, max_lat);

--
-- Name: virtual_project_areas_group_id_idx; Type: INDEX; Schema: agri; Owner: -
--

CREATE INDEX virtual_project_areas_group_id_idx ON agri.virtual_project_areas USING btree (group_id);

--
-- Name: virtual_project_areas_project_key_idx; Type: INDEX; Schema: agri; Owner: -
--

CREATE INDEX virtual_project_areas_project_key_idx ON agri.virtual_project_areas USING btree (project_key);

--
-- Name: ingest_batch_stats ingest_batch_stats_run_id_fkey; Type: FK CONSTRAINT; Schema: agri; Owner: -
--

ALTER TABLE ONLY agri.ingest_batch_stats
    ADD CONSTRAINT ingest_batch_stats_run_id_fkey FOREIGN KEY (run_id) REFERENCES agri.ingest_runs(run_id) ON DELETE CASCADE;

--
-- Name: ingested_oss_objects ingested_oss_objects_ingest_run_id_fkey; Type: FK CONSTRAINT; Schema: agri; Owner: -
--

ALTER TABLE ONLY agri.ingested_oss_objects
    ADD CONSTRAINT ingested_oss_objects_ingest_run_id_fkey FOREIGN KEY (ingest_run_id) REFERENCES agri.ingest_runs(run_id);

--
-- Name: ingested_oss_objects ingested_oss_objects_land_id_fk; Type: FK CONSTRAINT; Schema: agri; Owner: -
--

ALTER TABLE ONLY agri.ingested_oss_objects
    ADD CONSTRAINT ingested_oss_objects_land_id_fk FOREIGN KEY (land_id) REFERENCES agri.land_parcels(land_id);

--
-- Name: parcel_scene_products parcel_scene_products_land_id_fk; Type: FK CONSTRAINT; Schema: agri; Owner: -
--

ALTER TABLE ONLY agri.parcel_scene_products
    ADD CONSTRAINT parcel_scene_products_land_id_fk FOREIGN KEY (land_id) REFERENCES agri.land_parcels(land_id);

--
-- Name: virtual_project_area_lands virtual_project_area_lands_land_fk; Type: FK CONSTRAINT; Schema: agri; Owner: -
--

ALTER TABLE ONLY agri.virtual_project_area_lands
    ADD CONSTRAINT virtual_project_area_lands_land_fk FOREIGN KEY (land_id) REFERENCES agri.land_parcels(land_id) ON UPDATE CASCADE ON DELETE CASCADE;

--
-- Name: virtual_project_area_lands virtual_project_area_lands_tile_fk; Type: FK CONSTRAINT; Schema: agri; Owner: -
--

ALTER TABLE ONLY agri.virtual_project_area_lands
    ADD CONSTRAINT virtual_project_area_lands_tile_fk FOREIGN KEY (tile_id) REFERENCES agri.virtual_project_areas(tile_id) ON UPDATE CASCADE ON DELETE CASCADE;

--
-- PostgreSQL database dump complete
--

