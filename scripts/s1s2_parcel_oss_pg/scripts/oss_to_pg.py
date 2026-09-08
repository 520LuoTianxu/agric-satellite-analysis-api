#!/usr/bin/env python3
"""
从阿里云 OSS 拉取 S1/S2 地块产品 JSON，upsert 到 PostgreSQL。

改进自 agri_s1s2_parcel_bundle/scripts/oss_json_to_postgres.py：
  - 独立 schema（agri.*）+ 元数据视图 + ingest_runs
  - 更多列：land_name / parcel_cloud_cover_pct / clear_pixel_count / res_m / epsg / generated_at_shanghai
  - --apply-schema / --dry-run / --limit / OSS_ENV 可配
  - --from-done-dir / --keys-file：无 ListObjects 时按 key GetObject
  - 兼容 psycopg3 与 psycopg2
  - 每 50 条 commit，进度打印，错误计数

环境：
  OSS: OSS_ENV 或 /home/box/.config/aliyun/oss.env
  PG:  PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE

示例：
  python3 scripts/oss_to_pg.py --dry-run --limit 20
  python3 scripts/oss_to_pg.py --apply-schema --prefix s1s2_parcel/json/ --limit 100
  # 无 ListObjects 权限时：从本地 done 标记读 json_oss_key，仅 GetObject
  python3 scripts/oss_to_pg.py --from-done-dir output/machine-1/parcel_products --dry-run --limit 20
  python3 scripts/oss_to_pg.py --from-done-dir output/machine-1/parcel_products --apply-schema
  python3 scripts/oss_to_pg.py --keys-file keys.txt --limit 50
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Iterator, Optional

import oss2

# --- DB driver: prefer psycopg3, fallback psycopg2 ---
_PSYCOPG3 = False
try:
    import psycopg

    _PSYCOPG3 = True
except ImportError:  # pragma: no cover
    try:
        import psycopg2 as psycopg  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise SystemExit(
            "需要安装 psycopg[binary] 或 psycopg2-binary：pip install -r requirements.txt"
        ) from e

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OSS_ENV = Path.home() / ".config/aliyun/oss.env"
DEFAULT_PREFIX = "s1s2_parcel/json/"
SCHEMA_SQL = ROOT / "sql" / "001_schema.sql"
COMMIT_EVERY = 50

UPSERT_SQL = """
INSERT INTO agri.parcel_scene_products (
  parcel_id, tile_id, date, sensor, scene_id, land_name,
  cloud_cover, cloud_cover_over_30, parcel_cloud_cover_pct,
  json_oss_key, json_url, rgb_url, large_rgb_url, heatmap_url, s2_heatmap_url,
  vv_url, vh_url, pixel_count, clear_pixel_count, res_m, epsg,
  payload, generated_at_shanghai
) VALUES (
  %(parcel_id)s, %(tile_id)s, %(date)s, %(sensor)s, %(scene_id)s, %(land_name)s,
  %(cloud_cover)s, %(cloud_cover_over_30)s, %(parcel_cloud_cover_pct)s,
  %(json_oss_key)s, %(json_url)s, %(rgb_url)s, %(large_rgb_url)s,
  %(heatmap_url)s, %(s2_heatmap_url)s,
  %(vv_url)s, %(vh_url)s, %(pixel_count)s, %(clear_pixel_count)s, %(res_m)s, %(epsg)s,
  %(payload)s::jsonb, %(generated_at_shanghai)s
)
ON CONFLICT (parcel_id, date, sensor, scene_id) DO UPDATE SET
  tile_id = EXCLUDED.tile_id,
  land_name = EXCLUDED.land_name,
  cloud_cover = EXCLUDED.cloud_cover,
  cloud_cover_over_30 = EXCLUDED.cloud_cover_over_30,
  parcel_cloud_cover_pct = EXCLUDED.parcel_cloud_cover_pct,
  json_oss_key = EXCLUDED.json_oss_key,
  json_url = EXCLUDED.json_url,
  rgb_url = EXCLUDED.rgb_url,
  large_rgb_url = EXCLUDED.large_rgb_url,
  heatmap_url = EXCLUDED.heatmap_url,
  s2_heatmap_url = EXCLUDED.s2_heatmap_url,
  vv_url = EXCLUDED.vv_url,
  vh_url = EXCLUDED.vh_url,
  pixel_count = EXCLUDED.pixel_count,
  clear_pixel_count = EXCLUDED.clear_pixel_count,
  res_m = EXCLUDED.res_m,
  epsg = EXCLUDED.epsg,
  payload = EXCLUDED.payload,
  generated_at_shanghai = EXCLUDED.generated_at_shanghai,
  ingested_at = now()
"""


def load_dotenv_file(path: Path) -> None:
    """把 KEY=VALUE 写入 os.environ（不覆盖已有）。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def load_oss_cfg(oss_env: Path) -> dict:
    if not oss_env.is_file():
        raise FileNotFoundError(f"OSS 凭证文件不存在: {oss_env}")
    cfg: dict = {}
    for line in oss_env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip().strip('"').strip("'")
    required = ("OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_ENDPOINT", "OSS_BUCKET")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise ValueError(f"OSS 凭证缺少字段: {missing}")
    return cfg


def make_bucket(cfg: dict) -> oss2.Bucket:
    auth = oss2.Auth(cfg["OSS_ACCESS_KEY_ID"], cfg["OSS_ACCESS_KEY_SECRET"])
    return oss2.Bucket(auth, cfg["OSS_ENDPOINT"], cfg["OSS_BUCKET"])


def pg_connect():
    kwargs = dict(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=os.environ.get("PGPORT", "5432"),
        user=os.environ["PGUSER"],
        password=os.environ.get("PGPASSWORD", ""),
        dbname=os.environ.get("PGDATABASE", "postgres"),
    )
    return psycopg.connect(**kwargs)


def apply_schema(conn) -> None:
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print(f"[schema] applied {SCHEMA_SQL}", flush=True)


def iter_json_keys(bucket: oss2.Bucket, prefix: str, limit: int = 0) -> Iterator[str]:
    """列出 prefix 下 .json key。需要 ListObjects 权限。"""
    n = 0
    for obj in oss2.ObjectIterator(bucket, prefix=prefix):
        if not obj.key.endswith(".json"):
            continue
        yield obj.key
        n += 1
        if limit and n >= limit:
            break


def iter_keys_from_done_dir(done_root: Path, limit: int = 0) -> Iterator[str]:
    """从本地 pipeline done 标记读取 json_oss_key（不需 ListObjects）。

    期望路径形如：{done_root}/*/_done/parcel_*_S*.done
    （rglob 匹配 parcel_*_S*.done，且父目录名为 _done）
    文件为 JSON，含字段 json_oss_key。
    """
    seen: set[str] = set()
    n = 0
    paths = sorted(
        p for p in done_root.rglob("parcel_*_S*.done")
        if p.is_file() and p.parent.name == "_done"
    )
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        key = data.get("json_oss_key")
        if not key or not isinstance(key, str):
            continue
        key = key.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        yield key
        n += 1
        if limit and n >= limit:
            return


def iter_keys_from_file(path: Path, limit: int = 0) -> Iterator[str]:
    """从文本文件读取 OSS key：一行一个，# 开头为注释。"""
    seen: set[str] = set()
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line in seen:
            continue
        seen.add(line)
        yield line
        n += 1
        if limit and n >= limit:
            return


def row_from_product(obj: dict, key: str) -> dict:
    """从产品 JSON 映射到表行；payload 存完整对象。"""
    payload_str = json.dumps(obj, ensure_ascii=False)
    # psycopg2: 用字符串 + ::jsonb；psycopg3 同样可用字符串
    return {
        "parcel_id": str(obj.get("parcel_id") if obj.get("parcel_id") is not None else ""),
        "tile_id": obj.get("tile_id") or "",
        "date": obj.get("date"),
        "sensor": obj.get("sensor") or "",
        "scene_id": obj.get("scene_id") or "",
        "land_name": obj.get("land_name"),
        "cloud_cover": obj.get("cloud_cover"),
        "cloud_cover_over_30": obj.get("cloud_cover_over_30"),
        "parcel_cloud_cover_pct": obj.get("parcel_cloud_cover_pct"),
        "json_oss_key": obj.get("json_oss_key") or key,
        "json_url": obj.get("json_url"),
        "rgb_url": obj.get("rgb_url"),
        "large_rgb_url": obj.get("large_rgb_url"),
        "heatmap_url": obj.get("heatmap_url"),
        "s2_heatmap_url": obj.get("s2_heatmap_url"),
        "vv_url": obj.get("vv_url"),
        "vh_url": obj.get("vh_url"),
        "pixel_count": obj.get("pixel_count"),
        "clear_pixel_count": obj.get("clear_pixel_count"),
        "res_m": obj.get("res_m"),
        "epsg": obj.get("epsg"),
        "payload": payload_str,
        "generated_at_shanghai": obj.get("generated_at_shanghai"),
    }


def start_ingest_run(
    conn, prefix: str, bucket_name: str, limit_n: Optional[int], dry_run: bool
) -> Optional[int]:
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agri.ingest_runs (oss_prefix, oss_bucket, limit_n, dry_run, status)
                VALUES (%s, %s, %s, %s, 'running')
                RETURNING run_id
                """,
                (prefix, bucket_name, limit_n or None, dry_run),
            )
            row = cur.fetchone()
        conn.commit()
        return int(row[0]) if row else None
    except Exception as e:
        conn.rollback()
        print(f"[warn] ingest_runs 写入失败（可忽略）: {e}", flush=True)
        return None


def finish_ingest_run(
    conn,
    run_id: Optional[int],
    listed_n: int,
    upserted_n: int,
    error_n: int,
    status: str,
    notes: str = "",
) -> None:
    if run_id is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE agri.ingest_runs SET
                  finished_at = now(),
                  listed_n = %s,
                  upserted_n = %s,
                  error_n = %s,
                  status = %s,
                  notes = %s
                WHERE run_id = %s
                """,
                (listed_n, upserted_n, error_n, status, notes or None, run_id),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[warn] ingest_runs 更新失败: {e}", flush=True)


def upsert_one(cur, row: dict) -> None:
    cur.execute(UPSERT_SQL, row)


def resolve_key_iterator(args, bucket: oss2.Bucket) -> Iterator[str]:
    """按 CLI 选择 key 来源：done-dir / keys-file / OSS ListObjects。"""
    if args.from_done_dir:
        root = Path(args.from_done_dir).expanduser()
        if not root.is_dir():
            raise SystemExit(f"错误: --from-done-dir 不是目录: {root}")
        print(f"[keys] from done dir (GetObject only, no ListObjects): {root}", flush=True)
        return iter_keys_from_done_dir(root, args.limit)
    if args.keys_file:
        path = Path(args.keys_file).expanduser()
        if not path.is_file():
            raise SystemExit(f"错误: --keys-file 不存在: {path}")
        print(f"[keys] from file (GetObject only, no ListObjects): {path}", flush=True)
        return iter_keys_from_file(path, args.limit)
    print(
        f"[keys] listing OSS prefix={args.prefix!r} (requires ListObjects)",
        flush=True,
    )
    return iter_json_keys(bucket, args.prefix, args.limit)


def main(argv: Optional[list] = None) -> int:
    # 可选：项目根 .env（不含真实 OSS 密钥；仅 PG / OSS_ENV 路径）
    load_dotenv_file(ROOT / ".env")

    ap = argparse.ArgumentParser(
        description="OSS s1s2_parcel JSON → PostgreSQL agri.parcel_scene_products"
    )
    ap.add_argument(
        "--prefix",
        default=os.environ.get("OSS_PREFIX", DEFAULT_PREFIX),
        help=f"OSS 前缀（默认 {DEFAULT_PREFIX}；仅在未指定 --from-done-dir/--keys-file 时用于 ListObjects）",
    )
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 个 JSON（0=不限制）")
    ap.add_argument("--dry-run", action="store_true", help="仅列出 key，不写库")
    ap.add_argument(
        "--apply-schema",
        action="store_true",
        help=f"入库前执行 {SCHEMA_SQL.name}",
    )
    ap.add_argument(
        "--oss-env",
        default=os.environ.get("OSS_ENV", str(DEFAULT_OSS_ENV)),
        help="OSS 凭证文件路径",
    )
    ap.add_argument(
        "--from-done-dir",
        default=None,
        metavar="PATH",
        help="从本地 parcel_products 下 */_done/parcel_*_S*.done 读 json_oss_key；仅 GetObject，不 ListObjects",
    )
    ap.add_argument(
        "--keys-file",
        default=None,
        metavar="PATH",
        help="从文本文件读 OSS key（一行一个，# 注释）；仅 GetObject，不 ListObjects",
    )
    args = ap.parse_args(argv)

    if args.from_done_dir and args.keys_file:
        print("错误: --from-done-dir 与 --keys-file 不能同时使用", file=sys.stderr)
        return 2

    oss_env = Path(args.oss_env).expanduser()
    cfg = load_oss_cfg(oss_env)
    bucket = make_bucket(cfg)
    prefix = args.prefix
    # 记录来源标签（写入 ingest_runs.oss_prefix）
    if args.from_done_dir:
        source_label = f"done:{args.from_done_dir}"
    elif args.keys_file:
        source_label = f"keys:{args.keys_file}"
    else:
        source_label = prefix

    print(
        f"[oss] bucket={cfg['OSS_BUCKET']} endpoint={cfg['OSS_ENDPOINT']} prefix={prefix!r}",
        flush=True,
    )
    print(f"[db]  driver={'psycopg3' if _PSYCOPG3 else 'psycopg2'}", flush=True)

    key_iter = resolve_key_iterator(args, bucket)

    if args.dry_run:
        n = 0
        for key in key_iter:
            print(key)
            n += 1
        print(f"[dry-run] listed {n}", flush=True)
        return 0

    if "PGUSER" not in os.environ:
        print("错误: 请设置 PGUSER（以及 PGHOST/PGPASSWORD/PGDATABASE 等）", file=sys.stderr)
        return 2

    listed = upserted = errors = 0
    run_id: Optional[int] = None
    status = "ok"
    notes = ""

    with pg_connect() as conn:
        if args.apply_schema:
            apply_schema(conn)

        run_id = start_ingest_run(
            conn, source_label, cfg["OSS_BUCKET"], args.limit or None, dry_run=False
        )
        if run_id is not None:
            print(f"[run] ingest_runs.run_id={run_id}", flush=True)

        try:
            with conn.cursor() as cur:
                for key in key_iter:
                    listed += 1
                    try:
                        body = bucket.get_object(key).read()
                        data = json.loads(body)
                        if not isinstance(data, dict):
                            raise ValueError("JSON root 不是 object")
                        row = row_from_product(data, key)
                        if not row["parcel_id"] or not row["date"] or not row["sensor"]:
                            raise ValueError(
                                f"缺少必要字段 parcel_id/date/sensor: "
                                f"{row['parcel_id']!r} {row['date']!r} {row['sensor']!r}"
                            )
                        upsert_one(cur, row)
                        upserted += 1
                    except Exception as e:
                        errors += 1
                        print(f"[error] key={key}: {e}", flush=True)
                        if errors <= 3:
                            traceback.print_exc()
                        continue

                    if upserted % COMMIT_EVERY == 0:
                        conn.commit()
                        print(
                            f"[progress] listed={listed} upserted={upserted} errors={errors}",
                            flush=True,
                        )

                conn.commit()
        except Exception as e:
            status = "error"
            notes = str(e)
            conn.rollback()
            finish_ingest_run(conn, run_id, listed, upserted, errors, status, notes)
            raise

        if errors and status == "ok":
            status = "ok_with_errors"
            notes = f"{errors} object errors"
        finish_ingest_run(conn, run_id, listed, upserted, errors, status, notes)

    print(
        f"[DONE] listed={listed} upserted={upserted} errors={errors} status={status}",
        flush=True,
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
