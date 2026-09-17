#!/usr/bin/env python3
"""用业务 CSV 的 group_id 重写 farms.id，并导入/回写 land_parcels。

CSV 需要包含 group_id、group_name、land_id、wgs_land_path。脚本会：

1. 若 farms.id / land_parcels.farm_id 仍是 UUID，先改成 varchar(64)
2. 按唯一 group_id 每 100 条 upsert farms（id=group_id, name=group_name）
3. 按 land_id 每 100 条更新已有 land_parcels 的 farm_id / group_id / group_name
4. 把 CSV 有、库里没有的地块插入 land_parcels；无效边界跳过
5. 历史农场按 group_name 精确匹配，把旧 UUID farm_id 改成 group_id，再软删旧农场
   重名项目（同一名称多个 group_id）只走 land_id 映射，不做名称匹配

默认 dry-run。测试库示例：

  python scripts/remap_farms_from_group_csv.py --env-file ABflow/.env.test
  python scripts/remap_farms_from_group_csv.py --env-file ABflow/.env.test --apply
"""

from __future__ import annotations

import argparse
import csv
import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from psycopg2.extras import Json

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = Path(
    r"D:\download\2026-09-17-16-10-53_EXPORT_CSV_27941171_743"
    r"\2026-09-17-16-10-53_EXPORT_CSV_27941171_824_0.csv"
)
DEFAULT_ENV_FILES = (
    ROOT / "ABflow" / ".env.test.local",
    ROOT / "ABflow" / ".env.test",
    ROOT / "services" / "api" / ".env",
    ROOT / ".env",
)
SCHEMA_SQL = ROOT / "scripts" / "20260917_farm_id_to_text.sql"


@dataclass
class GroupRecord:
    group_id: str
    group_name: str
    land_ids: list[str] = field(default_factory=list)


@dataclass
class LandRow:
    land_id: str
    group_id: str
    group_name: str
    values: dict
    skip_reason: str | None = None


def _blank(value) -> str | None:
    text = "" if value is None else str(value).strip()
    return text or None


def path_to_polygon(raw: str | None) -> tuple[dict, tuple[float, float, float, float]] | None:
    if not raw or not str(raw).strip():
        return None
    ring: list[list[float]] = []
    for pair in str(raw).split("|"):
        parts = pair.split(",")
        if len(parts) != 2:
            continue
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError:
            continue
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            continue
        ring.append([lon, lat])
    if len(ring) < 3:
        return None
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    lons = [pt[0] for pt in ring]
    lats = [pt[1] for pt in ring]
    return (
        {"type": "Polygon", "coordinates": [ring]},
        (min(lons), min(lats), max(lons), max(lats)),
    )


def _load_env_file(path: Path, *, override: bool) -> None:
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
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
            "缺少 DATABASE_URL_SYNC / DATABASE_URL。请传 --env-file 或先导出连接串。"
        )
    return url


def _safe_db_target(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or "<missing-host>"
    port = parsed.port or 5432
    database = (parsed.path or "/").lstrip("/") or "<default>"
    return f"{host}:{port}/{database}"


def _chunks(items: list, size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def connect(url: str):
    try:
        import psycopg2
    except ImportError as exc:
        raise SystemExit("需要 psycopg2。请先安装 services/api 依赖。") from exc
    conn = psycopg2.connect(url)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO agric_satellite, public")
    conn.commit()
    return conn


def load_csv(
    path: Path,
) -> tuple[list[GroupRecord], dict[str, set[str]], list[LandRow], int]:
    if not path.is_file():
        raise SystemExit(f"找不到 CSV：{path}")

    by_id: dict[str, GroupRecord] = {}
    name_to_ids: dict[str, set[str]] = defaultdict(set)
    lands: list[LandRow] = []
    rows = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"group_id", "group_name", "land_id", "wgs_land_path"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"CSV 缺少列：{', '.join(sorted(missing))}")
        for index, row in enumerate(reader):
            rows += 1
            group_id = (row.get("group_id") or "").strip()
            group_name = (row.get("group_name") or "").strip()
            land_id = (row.get("land_id") or "").strip()
            if not group_id or not land_id:
                continue
            record = by_id.get(group_id)
            if record is None:
                record = GroupRecord(group_id=group_id, group_name=group_name)
                by_id[group_id] = record
            elif group_name and record.group_name != group_name:
                raise SystemExit(
                    f"group_id={group_id} 对应多个名称：{record.group_name!r} / {group_name!r}"
                )
            record.land_ids.append(land_id)
            if group_name:
                name_to_ids[group_name].add(group_id)

            parsed = path_to_polygon(row.get("wgs_land_path"))
            skip_reason = None
            boundary = None
            bounds = None
            if parsed is None:
                skip_reason = "invalid_or_empty_boundary"
            else:
                boundary, bounds = parsed
            area_mu = None
            area_text = _blank(row.get("land_area"))
            if area_text:
                try:
                    area_mu = float(area_text)
                except ValueError:
                    area_mu = None
            values = {
                "land_id": land_id,
                "source_parcel_id": land_id,
                "tile_id": f"manual_{land_id}",
                "farm_id": group_id,
                "land_name": _blank(row.get("land_name")) or land_id,
                "group_id": group_id,
                "group_name": group_name or None,
                "org_code": _blank(row.get("org_code")),
                "org_name": _blank(row.get("org_name")),
                "base_id": _blank(row.get("base_id")),
                "province_code": _blank(row.get("province_code")),
                "province_name": _blank(row.get("province_name")),
                "city_code": _blank(row.get("city_code")),
                "city_name": _blank(row.get("city_name")),
                "county_code": _blank(row.get("county_code")),
                "county_name": _blank(row.get("county_name")),
                "town_code": _blank(row.get("town_code")),
                "town_name": _blank(row.get("town_name")),
                "village_code": _blank(row.get("village_code")),
                "village_name": _blank(row.get("village_name")),
                "land_status": _blank(row.get("status")),
                "boundary_geojson": Json(boundary) if boundary else None,
                "boundary_srid": 4326,
                "min_lon": bounds[0] if bounds else None,
                "min_lat": bounds[1] if bounds else None,
                "max_lon": bounds[2] if bounds else None,
                "max_lat": bounds[3] if bounds else None,
                "area_ha": (area_mu / 15.0) if area_mu is not None else None,
                "original_area_mu": area_mu,
                "land_area_mu": area_mu,
                "source_properties": Json(
                    {
                        "source": "group_csv_import",
                        "group_code": _blank(row.get("group_code")),
                        "planting_type": _blank(row.get("planting_type")),
                        "business_category": _blank(row.get("business_category")),
                    }
                ),
                "source_file": path.name,
                "source_feature_index": index,
            }
            lands.append(
                LandRow(
                    land_id=land_id,
                    group_id=group_id,
                    group_name=group_name,
                    values=values,
                    skip_reason=skip_reason,
                )
            )
    return list(by_id.values()), dict(name_to_ids), lands, rows


def column_type(cur, table: str, column: str) -> str | None:
    cur.execute(
        """
        SELECT t.typname
          FROM pg_catalog.pg_attribute AS a
          JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
          JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
          JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
         WHERE n.nspname = 'agric_satellite'
           AND c.relname = %s
           AND a.attname = %s
           AND a.attnum > 0
           AND NOT a.attisdropped
        """,
        (table, column),
    )
    row = cur.fetchone()
    return row[0] if row else None


def apply_schema(conn, *, dry_run: bool) -> None:
    with conn.cursor() as cur:
        farm_type = column_type(cur, "farms", "id")
        parcel_type = column_type(cur, "land_parcels", "farm_id")
    print(f"[schema] farms.id={farm_type} land_parcels.farm_id={parcel_type}")
    if farm_type not in ("uuid", "varchar", "text", "bpchar"):
        raise SystemExit(f"未识别 farms.id 类型：{farm_type}")
    if farm_type != "uuid" and parcel_type != "uuid":
        print("[schema] 已是文本类型，跳过 ALTER")
        conn.commit()
        return
    print("[schema] 将执行 20260917_farm_id_to_text.sql")
    conn.commit()
    if dry_run:
        return
    sql = "\n".join(
        line
        for line in SCHEMA_SQL.read_text(encoding="utf-8").splitlines()
        if line.strip() not in {"BEGIN;", "COMMIT;"}
    )
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print("[schema] ALTER 完成")


def upsert_farms(conn, groups: list[GroupRecord], batch_size: int, *, dry_run: bool) -> int:
    sql = """
        INSERT INTO agric_satellite.farms (id, name, country, timezone, deleted_at)
        VALUES (%s, %s, 'CN', 'Asia/Shanghai', NULL)
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            deleted_at = NULL,
            updated_at = now()
    """
    total = 0
    for batch_no, batch in enumerate(_chunks(groups, batch_size), start=1):
        rows = [(item.group_id, item.group_name or item.group_id) for item in batch]
        print(
            f"[farms] batch={batch_no} size={len(rows)} "
            f"id={rows[0][0]}..{rows[-1][0]}"
        )
        if not dry_run:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
            conn.commit()
        total += len(rows)
    return total


def update_parcels_by_land(
    conn,
    groups: list[GroupRecord],
    batch_size: int,
    *,
    dry_run: bool,
) -> tuple[int, int]:
    sql = """
        UPDATE agric_satellite.land_parcels
           SET farm_id = %s,
               group_id = %s,
               group_name = %s,
               updated_at = now()
         WHERE land_id = %s
           AND deleted_at IS NULL
           AND (
                farm_id IS DISTINCT FROM %s
                OR group_id IS DISTINCT FROM %s
                OR group_name IS DISTINCT FROM %s
           )
    """
    payload: list[tuple[str, str, str, str, str, str, str]] = []
    for item in groups:
        name = item.group_name or item.group_id
        for land_id in item.land_ids:
            payload.append(
                (
                    item.group_id,
                    item.group_id,
                    name,
                    land_id,
                    item.group_id,
                    item.group_id,
                    name,
                )
            )

    updated = 0
    missing = 0
    for batch_no, batch in enumerate(_chunks(payload, batch_size), start=1):
        land_ids = [row[3] for row in batch]
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT land_id
                  FROM agric_satellite.land_parcels
                 WHERE land_id = ANY(%s)
                   AND deleted_at IS NULL
                """,
                (land_ids,),
            )
            existing = {row[0] for row in cur.fetchall()}
        present = [row for row in batch if row[3] in existing]
        missing += len(batch) - len(present)
        print(
            f"[parcels-by-land] batch={batch_no} csv={len(batch)} "
            f"in_db={len(present)} missing={len(batch) - len(present)}"
        )
        updated += len(present)
        if dry_run or not present:
            continue
        with conn.cursor() as cur:
            cur.executemany(sql, present)
        conn.commit()
    return updated, missing


INSERT_COLUMNS = (
    "land_id",
    "source_parcel_id",
    "tile_id",
    "farm_id",
    "land_name",
    "group_id",
    "group_name",
    "org_code",
    "org_name",
    "base_id",
    "province_code",
    "province_name",
    "city_code",
    "city_name",
    "county_code",
    "county_name",
    "town_code",
    "town_name",
    "village_code",
    "village_name",
    "land_status",
    "boundary_geojson",
    "boundary_srid",
    "min_lon",
    "min_lat",
    "max_lon",
    "max_lat",
    "area_ha",
    "original_area_mu",
    "land_area_mu",
    "source_properties",
    "source_file",
    "source_feature_index",
)


def parcel_columns(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.attname
              FROM pg_catalog.pg_attribute AS a
              JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
              JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
             WHERE n.nspname = 'agric_satellite'
               AND c.relname = 'land_parcels'
               AND a.attnum > 0
               AND NOT a.attisdropped
            """
        )
        return {row[0] for row in cur.fetchall()}


def insert_missing_parcels(
    conn,
    lands: list[LandRow],
    batch_size: int,
    *,
    dry_run: bool,
) -> tuple[int, int, int]:
    skipped = [item for item in lands if item.skip_reason]
    valid = [item for item in lands if not item.skip_reason]
    print(
        f"[parcels-insert] valid={len(valid)} skipped_invalid_boundary={len(skipped)}",
        flush=True,
    )
    if skipped:
        print(
            "[parcels-insert] skip_land_ids="
            + ",".join(item.land_id for item in skipped[:20]),
            flush=True,
        )

    with conn.cursor() as cur:
        cur.execute("SELECT land_id FROM agric_satellite.land_parcels")
        existing = {str(row[0]).strip() for row in cur.fetchall() if row[0] is not None}
        cur.execute(
            """
            SELECT land_id
              FROM agric_satellite.land_parcels
             WHERE deleted_at IS NOT NULL
            """
        )
        deleted_ids = {
            str(row[0]).strip() for row in cur.fetchall() if row[0] is not None
        }
    seen: set[str] = set()
    to_insert = []
    for item in valid:
        land_id = item.land_id.strip()
        if land_id in existing or land_id in seen:
            continue
        seen.add(land_id)
        to_insert.append(item)
    to_revive = [item for item in valid if item.land_id in deleted_ids]
    print(
        f"[parcels-insert] existing={len(existing)} insert={len(to_insert)} "
        f"revive_deleted={len(to_revive)}",
        flush=True,
    )

    columns = [name for name in INSERT_COLUMNS if name in parcel_columns(conn)]
    insert_sql = (
        "INSERT INTO agric_satellite.land_parcels ("
        + ", ".join(columns)
        + ") VALUES ("
        + ", ".join(f"%({name})s" for name in columns)
        + ") ON CONFLICT (land_id) DO NOTHING"
    )
    revive_sql = """
        UPDATE agric_satellite.land_parcels
           SET farm_id = %(farm_id)s,
               group_id = %(group_id)s,
               group_name = %(group_name)s,
               land_name = %(land_name)s,
               boundary_geojson = %(boundary_geojson)s,
               boundary_srid = %(boundary_srid)s,
               min_lon = %(min_lon)s,
               min_lat = %(min_lat)s,
               max_lon = %(max_lon)s,
               max_lat = %(max_lat)s,
               deleted_at = NULL,
               updated_at = now()
         WHERE land_id = %(land_id)s
           AND deleted_at IS NOT NULL
    """

    inserted = 0
    revived = 0
    for batch_no, batch in enumerate(_chunks(to_insert, batch_size), start=1):
        print(f"[parcels-insert] batch={batch_no} insert={len(batch)}", flush=True)
        if dry_run:
            inserted += len(batch)
            continue
        with conn.cursor() as cur:
            cur.executemany(insert_sql, [item.values for item in batch])
        conn.commit()
        inserted += len(batch)
    for batch_no, batch in enumerate(_chunks(to_revive, batch_size), start=1):
        print(f"[parcels-revive] batch={batch_no} revive={len(batch)}", flush=True)
        if dry_run:
            revived += len(batch)
            continue
        with conn.cursor() as cur:
            cur.executemany(revive_sql, [item.values for item in batch])
        conn.commit()
        revived += len(batch)
    return inserted, revived, len(skipped)


def remap_old_farms_by_name(
    conn,
    groups: list[GroupRecord],
    name_to_ids: dict[str, set[str]],
    batch_size: int,
    *,
    dry_run: bool,
) -> tuple[int, int, list[str]]:
    ambiguous = sorted(
        name for name, ids in name_to_ids.items() if len(ids) > 1
    )
    unique_groups = [
        item
        for item in groups
        if item.group_name and len(name_to_ids.get(item.group_name, set())) == 1
    ]
    parcel_sql = """
        UPDATE agric_satellite.land_parcels AS lp
           SET farm_id = %s,
               group_id = %s,
               group_name = %s,
               updated_at = now()
          FROM agric_satellite.farms AS f
         WHERE lp.farm_id = f.id
           AND f.deleted_at IS NULL
           AND f.id <> %s
           AND f.name = %s
           AND lp.deleted_at IS NULL
    """
    farm_sql = """
        UPDATE agric_satellite.farms
           SET deleted_at = now(),
               updated_at = now()
         WHERE deleted_at IS NULL
           AND id <> %s
           AND name = %s
    """
    parcels = 0
    farms = 0
    for batch_no, batch in enumerate(_chunks(unique_groups, batch_size), start=1):
        print(f"[name-remap] batch={batch_no} size={len(batch)}")
        if dry_run:
            with conn.cursor() as cur:
                names = [item.group_name for item in batch]
                cur.execute(
                    """
                    SELECT id, name
                      FROM agric_satellite.farms
                     WHERE deleted_at IS NULL
                       AND name = ANY(%s)
                    """,
                    (names,),
                )
                old_rows = [
                    (farm_id, name)
                    for farm_id, name in cur.fetchall()
                    if str(farm_id) not in name_to_ids.get(name, set())
                ]
            print(f"[name-remap] dry-run old_farms_in_batch={len(old_rows)}")
            farms += len(old_rows)
            continue
        with conn.cursor() as cur:
            for item in batch:
                cur.execute(
                    parcel_sql,
                    (
                        item.group_id,
                        item.group_id,
                        item.group_name,
                        item.group_id,
                        item.group_name,
                    ),
                )
                parcels += cur.rowcount or 0
                cur.execute(farm_sql, (item.group_id, item.group_name))
                farms += cur.rowcount or 0
        conn.commit()
    return parcels, farms, ambiguous


def summarize(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM agric_satellite.farms
             WHERE deleted_at IS NULL AND id ~ '^[0-9]+$'
            """
        )
        numeric_farms = cur.fetchone()[0]
        cur.execute(
            """
            SELECT count(*) FROM agric_satellite.land_parcels
             WHERE deleted_at IS NULL AND farm_id ~ '^[0-9]+$'
            """
        )
        numeric_parcels = cur.fetchone()[0]
        cur.execute(
            """
            SELECT count(*) FROM agric_satellite.land_parcels
             WHERE deleted_at IS NULL
               AND farm_id IS NOT NULL
               AND farm_id !~ '^[0-9]+$'
            """
        )
        leftover = cur.fetchone()[0]
    print(
        f"[summary] numeric_farms={numeric_farms} "
        f"numeric_parcel_farm_id={numeric_parcels} leftover_uuid_farm_id={leftover}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="带 group_id 的地块 CSV")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="测试库 dotenv。默认依次尝试 ABflow/.env.test.local、.env.test、services/api/.env、.env",
    )
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写入测试库。不加该参数只预览。",
    )
    parser.add_argument(
        "--insert-missing-only",
        action="store_true",
        help="只插入 CSV 中库里还没有的地块，不再改 farms / 已有 land_parcels",
    )
    args = parser.parse_args()
    if args.batch_size <= 0:
        raise SystemExit("--batch-size 必须大于 0")

    if args.env_file:
        _load_env_file(args.env_file, override=True)
    else:
        for path in DEFAULT_ENV_FILES:
            _load_env_file(path, override=False)

    url = _database_url()
    dry_run = not args.apply
    groups, name_to_ids, lands, csv_rows = load_csv(args.csv)
    print(f"[csv] path={args.csv}")
    print(f"[csv] rows={csv_rows} unique_groups={len(groups)}")
    print(f"[db] target={_safe_db_target(url)} dry_run={dry_run} batch_size={args.batch_size}")

    conn = connect(url)
    try:
        apply_schema(conn, dry_run=dry_run)
        farm_n = upsert_farms(conn, groups, args.batch_size, dry_run=dry_run)
        parcel_n = 0
        missing_n = 0
        name_parcels = 0
        old_farms = 0
        ambiguous: list[str] = []
        if not args.insert_missing_only:
            parcel_n, missing_n = update_parcels_by_land(
                conn, groups, args.batch_size, dry_run=dry_run
            )
        inserted_n, revived_n, skipped_n = insert_missing_parcels(
            conn, lands, args.batch_size, dry_run=dry_run
        )
        if not args.insert_missing_only:
            name_parcels, old_farms, ambiguous = remap_old_farms_by_name(
                conn, groups, name_to_ids, args.batch_size, dry_run=dry_run
            )
        if not dry_run:
            summarize(conn)
    finally:
        conn.close()

    print("[done]")
    print(f"  farms_upserted={farm_n}")
    print(f"  parcels_updated_by_land_id={parcel_n}")
    print(f"  parcels_inserted={inserted_n}")
    print(f"  parcels_revived_from_deleted={revived_n}")
    print(f"  parcels_skipped_invalid_boundary={skipped_n}")
    print(f"  csv_lands_not_in_db_before_insert={missing_n}")
    print(f"  parcels_remapped_by_group_name={name_parcels}")
    print(f"  old_farms_soft_deleted={old_farms}")
    if ambiguous:
        print("  skipped_ambiguous_group_names:")
        for name in ambiguous:
            print(f"    {name}: {sorted(name_to_ids[name])}")
    if dry_run:
        print("这是预览。确认目标库后加上 --apply 再刷测试库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
