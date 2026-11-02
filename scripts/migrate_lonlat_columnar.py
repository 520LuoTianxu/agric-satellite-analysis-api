#!/usr/bin/env python3
"""Inventory / migrate parcel scene lonlat JSON: lonlat_v1 → lonlat_columns_v1.

Default mode is **dry-run** (no OSS put, no DB update). Production convert must
be explicitly opted in with ``--apply`` after deploying dual-read code.

Examples (all dry-run unless ``--apply``):

  # Count OSS keys by format (samples bodies; uses ListObjects or a keys file)
  python3 scripts/migrate_lonlat_columnar.py inventory-oss --limit 200
  python3 scripts/migrate_lonlat_columnar.py inventory-oss --keys-file keys.txt

  # Count DB pixel_data by format
  python3 scripts/migrate_lonlat_columnar.py inventory-db

  # Convert one OSS object (still dry-run without --apply)
  python3 scripts/migrate_lonlat_columnar.py convert-oss --key s1s2_parcel/json/7750/2026-10-19_S2.json
  python3 scripts/migrate_lonlat_columnar.py convert-oss --keys-file keys.txt --limit 50 --concurrency 6 --apply

  # Convert DB rows (pixel_data jsonb only)
  python3 scripts/migrate_lonlat_columnar.py convert-db --limit 100
  python3 scripts/migrate_lonlat_columnar.py convert-db --land-id 7750 --apply

Env (same as other OSS scripts):
  OSS_ENV / ~/.config/aliyun/oss.env / .env
  OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET, OSS_ENDPOINT, OSS_BUCKET
  Optional: OSS_ACCELERATE_ENDPOINT=https://oss-accelerate.aliyuncs.com
            (or bucket.agric-prod.oss-accelerate.aliyuncs.com style via endpoint)
  PG*: PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "agric_satellite_analysis_common"))

from agric_satellite_analysis_common.lonlat_pixels import (  # noqa: E402
    LONLAT_FORMAT_COLUMNS_V1,
    LONLAT_FORMAT_V1,
    extract_pixels,
    migrate_pixel_data_to_columnar,
    migrate_product_to_columnar,
    needs_columnar_migration,
    pixel_count,
)

DEFAULT_OSS_ENV = Path.home() / ".config/aliyun/oss.env"
DEFAULT_PREFIX = "s1s2_parcel/json/"
DEFAULT_PART_SIZE = 4 * 1024 * 1024
DEFAULT_MULTIPART_THRESHOLD = 5 * 1024 * 1024


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = val


def load_oss_env() -> dict[str, str]:
    for cand in (
        Path(os.environ.get("OSS_ENV", "")),
        DEFAULT_OSS_ENV,
        REPO_ROOT / ".env",
    ):
        if cand and str(cand) not in ("", "."):
            _load_env_file(cand)
    ak = os.environ.get("OSS_ACCESS_KEY_ID", "")
    sk = os.environ.get("OSS_ACCESS_KEY_SECRET", "")
    if not ak or not sk:
        raise SystemExit(
            "缺少 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET（OSS_ENV 或 ~/.config/aliyun/oss.env）"
        )
    endpoint = (
        os.environ.get("OSS_ACCELERATE_ENDPOINT")
        or os.environ.get("OSS_ENDPOINT")
        or "https://oss-cn-beijing.aliyuncs.com"
    )
    bucket = os.environ.get("OSS_BUCKET", "agric-prod")
    return {"ak": ak, "sk": sk, "endpoint": endpoint, "bucket": bucket}


def oss_bucket():
    import oss2

    cfg = load_oss_env()
    auth = oss2.Auth(cfg["ak"], cfg["sk"])
    return oss2.Bucket(auth, cfg["endpoint"], cfg["bucket"]), cfg


def pg_connect():
    try:
        import psycopg

        return psycopg.connect(
            host=os.environ.get("PGHOST", "127.0.0.1"),
            port=int(os.environ.get("PGPORT", "5432")),
            user=os.environ.get("PGUSER", "postgres"),
            password=os.environ.get("PGPASSWORD", ""),
            dbname=os.environ.get("PGDATABASE", "postgres"),
        ), "psycopg3"
    except ImportError:
        import psycopg2

        return psycopg2.connect(
            host=os.environ.get("PGHOST", "127.0.0.1"),
            port=int(os.environ.get("PGPORT", "5432")),
            user=os.environ.get("PGUSER", "postgres"),
            password=os.environ.get("PGPASSWORD", ""),
            dbname=os.environ.get("PGDATABASE", "postgres"),
        ), "psycopg2"


def classify_format(obj: Any) -> str:
    """Return lonlat_columns_v1 | lonlat_v1 | root_pixels | other | empty."""
    if not isinstance(obj, dict):
        return "other"
    nested = obj.get("pixel_data")
    if isinstance(nested, dict):
        fmt = nested.get("format")
        if fmt == LONLAT_FORMAT_COLUMNS_V1:
            return LONLAT_FORMAT_COLUMNS_V1
        if fmt == LONLAT_FORMAT_V1 or isinstance(nested.get("pixels"), list):
            return LONLAT_FORMAT_V1
        if isinstance(nested.get("columns"), dict):
            return LONLAT_FORMAT_COLUMNS_V1
    fmt = obj.get("format")
    if fmt == LONLAT_FORMAT_COLUMNS_V1:
        return LONLAT_FORMAT_COLUMNS_V1
    if fmt == LONLAT_FORMAT_V1:
        return LONLAT_FORMAT_V1
    if isinstance(obj.get("pixels"), list):
        return "root_pixels"
    if isinstance(obj.get("columns"), dict):
        return LONLAT_FORMAT_COLUMNS_V1
    return "other"


def iter_keys_from_file(path: Path, *, limit: int = 0) -> Iterator[str]:
    n = 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key = raw.strip()
        if not key or key.startswith("#"):
            continue
        yield key
        n += 1
        if limit and n >= limit:
            return


def iter_keys_list(bucket, prefix: str, *, limit: int = 0, suffix: str = ".json") -> Iterator[str]:
    import oss2

    n = 0
    for obj in oss2.ObjectIterator(bucket, prefix=prefix):
        key = obj.key
        if suffix and not key.endswith(suffix):
            continue
        if key.endswith("/"):
            continue
        yield key
        n += 1
        if limit and n >= limit:
            return


def load_state(path: Path | None) -> set[str]:
    if path is None or not path.is_file():
        return set()
    done: set[str] = set()
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            done.add(line.split("\t", 1)[0])
    return done


def append_state(path: Path | None, key: str, status: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{key}\t{status}\t{int(time.time())}\n")


def get_json(bucket, key: str) -> dict[str, Any]:
    raw = bucket.get_object(key).read()
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError(f"OSS object is not a JSON object: {key}")
    return obj


def put_json(
    bucket,
    key: str,
    obj: dict[str, Any],
    *,
    part_size: int = DEFAULT_PART_SIZE,
    multipart_threshold: int = DEFAULT_MULTIPART_THRESHOLD,
) -> int:
    """Upload JSON via put_object or resumable multipart for larger bodies."""
    import oss2

    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if len(body) < multipart_threshold:
        bucket.put_object(key, body, headers=headers)
        return len(body)

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp.write(body)
        tmp_path = tmp.name
    try:
        oss2.resumable_upload(
            bucket,
            key,
            tmp_path,
            headers=headers,
            multipart_threshold=multipart_threshold,
            part_size=part_size,
            num_threads=4,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return len(body)


def cmd_inventory_oss(args: argparse.Namespace) -> int:
    bucket, cfg = oss_bucket()
    print(f"bucket={cfg['bucket']} endpoint={cfg['endpoint']} prefix={args.prefix}")
    if args.keys_file:
        keys = list(iter_keys_from_file(Path(args.keys_file), limit=args.limit))
    else:
        keys = list(iter_keys_list(bucket, args.prefix, limit=args.limit))
    print(f"keys_listed={len(keys)}")

    counts: dict[str, int] = {}
    need_migrate = 0
    errors = 0
    sample_old: list[str] = []
    legacy_all: list[str] = []
    lock = threading.Lock()

    def one(key: str) -> None:
        nonlocal need_migrate, errors
        try:
            obj = get_json(bucket, key)
            fmt = classify_format(obj)
            mig = needs_columnar_migration(obj)
            with lock:
                counts[fmt] = counts.get(fmt, 0) + 1
                if mig:
                    need_migrate += 1
                    legacy_all.append(key)
                    if len(sample_old) < args.sample:
                        sample_old.append(key)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors += 1
                counts["error"] = counts.get("error", 0) + 1
            if errors <= 5:
                print(f"ERR {key}: {type(exc).__name__}: {exc}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futs = [pool.submit(one, k) for k in keys]
        for i, fut in enumerate(as_completed(futs), 1):
            fut.result()
            if i % 50 == 0 or i == len(futs):
                print(
                    f"... inventoried {i}/{len(keys)} need_migrate={need_migrate} errors={errors}",
                    flush=True,
                )

    print("format_counts:", json.dumps(counts, ensure_ascii=False, sort_keys=True))
    print(f"need_migrate={need_migrate} errors={errors}")
    if sample_old:
        print("sample_legacy_keys:")
        for k in sample_old:
            print(f"  {k}")
    if args.write_legacy_keys:
        Path(args.write_legacy_keys).write_text(
            "\n".join(legacy_all) + ("\n" if legacy_all else ""),
            encoding="utf-8",
        )
        print(f"wrote_legacy_keys={args.write_legacy_keys} n={len(legacy_all)}")
    return 0


def cmd_inventory_db(args: argparse.Namespace) -> int:
    conn, driver = pg_connect()
    print(f"pg_driver={driver}")
    sql = """
    SELECT
      COALESCE(pixel_data->>'format', '(null)') AS fmt,
      count(*) AS n,
      count(*) FILTER (
        WHERE pixel_data->>'format' = 'lonlat_v1'
           OR (
             COALESCE(pixel_data->>'format','') <> 'lonlat_columns_v1'
             AND jsonb_typeof(pixel_data->'pixels') = 'array'
           )
      ) AS need_migrate_est
    FROM agric_satellite.parcel_scene_products
    WHERE pixel_data IS NOT NULL
    GROUP BY 1
    ORDER BY n DESC
    """
    with conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    print("fmt\tn\tneed_migrate_est_in_group")
    total = 0
    need = 0
    for fmt, n, nm in rows:
        print(f"{fmt}\t{n}\t{nm}")
        total += int(n)
        need += int(nm or 0)
    print(f"total_with_pixel_data={total} need_migrate_est={need}")
    print(
        "注: need_migrate_est 按 format/pixels 粗估；"
        "真正转换前请用 convert-db --dry-run（默认）逐行走 Python helper。"
    )
    return 0


def _convert_one_oss(
    bucket,
    key: str,
    *,
    apply: bool,
    part_size: int,
    multipart_threshold: int,
) -> dict[str, Any]:
    obj = get_json(bucket, key)
    before = classify_format(obj)
    before_n = pixel_count(obj.get("pixel_data") if isinstance(obj.get("pixel_data"), dict) else obj)
    if not needs_columnar_migration(obj):
        return {
            "key": key,
            "status": "skip_already_columnar",
            "format_before": before,
            "pixels": before_n,
        }
    # Round-trip guard: extract before mutate
    pixels_before = extract_pixels(
        obj["pixel_data"] if isinstance(obj.get("pixel_data"), dict) else obj
    )
    changed = migrate_product_to_columnar(obj)
    if not changed:
        return {"key": key, "status": "skip_no_change", "format_before": before}
    pixels_after = extract_pixels(obj.get("pixel_data") or obj)
    if len(pixels_after) != len(pixels_before):
        raise RuntimeError(
            f"pixel count changed {len(pixels_before)} -> {len(pixels_after)} for {key}"
        )
    for a, b in zip(pixels_before, pixels_after):
        if a != b:
            raise RuntimeError(f"pixel value mismatch after convert for {key}")
    after = classify_format(obj)
    size = None
    if apply:
        size = put_json(
            bucket,
            key,
            obj,
            part_size=part_size,
            multipart_threshold=multipart_threshold,
        )
    return {
        "key": key,
        "status": "converted" if apply else "would_convert",
        "format_before": before,
        "format_after": after,
        "pixels": len(pixels_after),
        "bytes_written": size,
        "applied": apply,
    }


def cmd_convert_oss(args: argparse.Namespace) -> int:
    bucket, cfg = oss_bucket()
    print(
        f"bucket={cfg['bucket']} endpoint={cfg['endpoint']} apply={args.apply} "
        f"concurrency={args.concurrency}"
    )
    keys: list[str] = []
    if args.key:
        keys.append(args.key)
    if args.keys_file:
        keys.extend(iter_keys_from_file(Path(args.keys_file), limit=0))
    if not keys and args.prefix:
        keys.extend(iter_keys_list(bucket, args.prefix, limit=args.limit or 0))
    if args.limit:
        keys = keys[: args.limit]
    done = load_state(Path(args.state_file) if args.state_file else None)
    keys = [k for k in keys if k not in done]
    print(f"keys_todo={len(keys)} (skipped_done={len(done)})")

    ok = err = skip = 0
    lock = threading.Lock()
    state_path = Path(args.state_file) if args.state_file else None

    def one(key: str) -> None:
        nonlocal ok, err, skip
        try:
            result = _convert_one_oss(
                bucket,
                key,
                apply=args.apply,
                part_size=args.part_size,
                multipart_threshold=args.multipart_threshold,
            )
            status = result["status"]
            with lock:
                if status.startswith("skip"):
                    skip += 1
                else:
                    ok += 1
                append_state(state_path, key, status)
            if args.verbose or status.startswith("would") or status == "converted":
                print(json.dumps(result, ensure_ascii=False), flush=True)
        except Exception as exc:  # noqa: BLE001
            with lock:
                err += 1
                append_state(state_path, key, f"error:{type(exc).__name__}")
            print(f"ERR {key}: {exc}", file=sys.stderr)
            if args.verbose:
                traceback.print_exc()

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futs = [pool.submit(one, k) for k in keys]
        for i, fut in enumerate(as_completed(futs), 1):
            fut.result()
            if i % 20 == 0 or i == len(futs):
                print(f"... {i}/{len(keys)} ok={ok} skip={skip} err={err}", flush=True)

    print(f"done ok={ok} skip={skip} err={err} apply={args.apply}")
    if not args.apply:
        print("dry-run only — 加 --apply 才会写回 OSS（部署 dual-read 后再执行）")
    return 0 if err == 0 else 1


def cmd_convert_db(args: argparse.Namespace) -> int:
    conn, driver = pg_connect()
    print(f"pg_driver={driver} apply={args.apply}")
    where = ["pixel_data IS NOT NULL"]
    params: list[Any] = []
    if args.land_id:
        where.append("land_id = %s")
        params.append(args.land_id)
    if args.sensor:
        where.append("sensor = %s")
        params.append(args.sensor)
    # Prefer rows that still look like object-list.
    where.append(
        """(
          pixel_data->>'format' = 'lonlat_v1'
          OR (
            COALESCE(pixel_data->>'format','') <> 'lonlat_columns_v1'
            AND jsonb_typeof(pixel_data->'pixels') = 'array'
          )
          OR (
            pixel_data->>'format' = 'lonlat_columns_v1'
            AND pixel_data ? 'pixels'
          )
        )"""
    )
    sql = f"""
    SELECT land_id, date::text, sensor, scene_id, pixel_data
    FROM agric_satellite.parcel_scene_products
    WHERE {' AND '.join(where)}
    ORDER BY land_id, date, sensor, scene_id
    """
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"

    with conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    print(f"rows_fetched={len(rows)}")

    would = applied = skipped = errors = 0
    for land_id, date_str, sensor, scene_id, pixel_data in rows:
        try:
            if isinstance(pixel_data, str):
                pixel_data = json.loads(pixel_data)
            if not isinstance(pixel_data, dict):
                skipped += 1
                continue
            before_pixels = extract_pixels(pixel_data)
            if not needs_columnar_migration(pixel_data):
                skipped += 1
                continue
            pd = dict(pixel_data)
            if not migrate_pixel_data_to_columnar(pd):
                skipped += 1
                continue
            after_pixels = extract_pixels(pd)
            if after_pixels != before_pixels:
                raise RuntimeError("roundtrip mismatch")
            if args.apply:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE agric_satellite.parcel_scene_products
                            SET pixel_data = %s::jsonb
                            WHERE land_id = %s AND date = %s::date
                              AND sensor = %s AND scene_id = %s
                            """,
                            (
                                json.dumps(pd, ensure_ascii=False, separators=(",", ":")),
                                land_id,
                                date_str,
                                sensor,
                                scene_id,
                            ),
                        )
                applied += 1
            else:
                would += 1
            if args.verbose:
                print(
                    json.dumps(
                        {
                            "land_id": land_id,
                            "date": date_str,
                            "sensor": sensor,
                            "scene_id": scene_id,
                            "pixels": len(after_pixels),
                            "status": "converted" if args.apply else "would_convert",
                        },
                        ensure_ascii=False,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(
                f"ERR land={land_id} date={date_str} sensor={sensor} scene={scene_id}: {exc}",
                file=sys.stderr,
            )
    print(
        f"done would_convert={would} applied={applied} skipped={skipped} errors={errors} apply={args.apply}"
    )
    if not args.apply:
        print("dry-run only — 加 --apply 才会 UPDATE（部署 dual-read 后再执行）")
    return 0 if errors == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    inv = sub.add_parser("inventory-oss", help="统计 OSS 场景 JSON 的 format（默认抽样/限量）")
    inv.add_argument("--prefix", default=DEFAULT_PREFIX)
    inv.add_argument("--keys-file", default=None, help="每行一个 OSS key；无 ListObjects 权限时用")
    inv.add_argument("--limit", type=int, default=200)
    inv.add_argument("--concurrency", type=int, default=8)
    inv.add_argument("--sample", type=int, default=20, help="打印多少条旧格式样例 key")
    inv.add_argument("--write-legacy-keys", default=None, help="把需迁移的 key 写入该文件")
    inv.set_defaults(func=cmd_inventory_oss)

    idb = sub.add_parser("inventory-db", help="按 pixel_data.format 统计 DB 行数")
    idb.set_defaults(func=cmd_inventory_db)

    co = sub.add_parser("convert-oss", help="把 OSS 场景 JSON 原地转为 lonlat_columns_v1（默认 dry-run）")
    co.add_argument("--key", default=None)
    co.add_argument("--keys-file", default=None)
    co.add_argument("--prefix", default=DEFAULT_PREFIX)
    co.add_argument("--limit", type=int, default=0)
    co.add_argument("--concurrency", type=int, default=6)
    co.add_argument("--apply", action="store_true", help="真正写回 OSS；缺省只 dry-run")
    co.add_argument("--state-file", default=None, help="断点续跑：已处理 key 追加写入")
    co.add_argument("--part-size", type=int, default=DEFAULT_PART_SIZE)
    co.add_argument("--multipart-threshold", type=int, default=DEFAULT_MULTIPART_THRESHOLD)
    co.add_argument("--verbose", action="store_true")
    co.set_defaults(func=cmd_convert_oss)

    cd = sub.add_parser("convert-db", help="把 parcel_scene_products.pixel_data 转为列式（默认 dry-run）")
    cd.add_argument("--land-id", default=None)
    cd.add_argument("--sensor", default=None)
    cd.add_argument("--limit", type=int, default=100)
    cd.add_argument("--apply", action="store_true")
    cd.add_argument("--verbose", action="store_true")
    cd.set_defaults(func=cmd_convert_db)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
