#!/usr/bin/env python3
"""Ensure canonical land parcels have soil and weather data.

The only parcel identity accepted by this utility is
``agric_satellite.land_parcels.land_id``.  Farm filtering uses the optional
``farm_id`` ownership relation; it never joins a legacy fields table, parses
tags, or creates a parcel-to-parcel mapping.

Usage (from the repository root, with the compose stack running)::

    # Dry-run (default): show canonical parcels missing data
    python3 scripts/agri_seed/ensure_land_soil_weather.py

    # Enqueue missing tasks in the ingest container
    python3 scripts/agri_seed/ensure_land_soil_weather.py --apply
    python3 scripts/agri_seed/ensure_land_soil_weather.py --apply --all
    python3 scripts/agri_seed/ensure_land_soil_weather.py --land-id 25107
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys


DEFAULT_FARM = "范莘·lonlat_v1样例"
DB_CONTAINER = os.environ.get("DOCKER_DB_CONTAINER", "agric-satellite-analysis-db-1")
TASK_CONTAINER = os.environ.get(
    "DOCKER_TASK_CONTAINER",
    os.environ.get("DOCKER_INGEST_CONTAINER", "agric-satellite-analysis-ingest-1"),
)
PGUSER = os.environ.get("POSTGRES_USER", "openfarm")
PGDB = os.environ.get("POSTGRES_DB", "openfarm")


def _sql_literal(value: str) -> str:
    """Quote a CLI value before embedding it in the read-only inspection SQL."""

    return "'" + value.replace("'", "''") + "'"


def psql(sql: str) -> str:
    r = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "-e",
            "PGOPTIONS=-csearch_path=agric_satellite",
            DB_CONTAINER,
            "psql",
            "-U",
            PGUSER,
            "-d",
            PGDB,
            "-v",
            "ON_ERROR_STOP=1",
            "-t",
            "-A",
            "-F",
            "\t",
            "-c",
            sql,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def celery_delay(task: str, land_id: str) -> None:
    """Dispatch a canonical land task inside the ingest image."""

    task_names = {
        "soil": "app.tasks.soil.fetch_soil_for_land",
        "weather": "app.tasks.weather.backfill_weather_for_land",
    }
    try:
        task_name = task_names[task]
    except KeyError as exc:
        raise ValueError(task) from exc

    # 任务参数直接使用主表 land_id，避免 worker 再做身份推导。
    code = (
        "from celery import current_app; "
        f"print(current_app.send_task({task_name!r}, args=[{land_id!r}]).id)"
    )
    subprocess.run(
        ["docker", "exec", "-i", TASK_CONTAINER, "python", "-c", code],
        check=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Enqueue missing soil/weather tasks")
    ap.add_argument("--all", action="store_true", help="Check all canonical parcels")
    ap.add_argument("--farm", default=DEFAULT_FARM, help="Farm name filter")
    ap.add_argument(
        "--land-id",
        action="append",
        default=[],
        help="Limit to canonical land_id; may be supplied more than once",
    )
    args = ap.parse_args()

    where = ["p.deleted_at IS NULL"]
    if args.land_id:
        ids = ",".join(_sql_literal(value) for value in args.land_id)
        where.append(f"p.land_id IN ({ids})")
    elif not args.all:
        where.append(f"fm.name = {_sql_literal(args.farm)}")

    sql = f"""
SELECT p.land_id,
       COALESCE(p.land_name, ''),
       COALESCE(fm.name, ''),
       (SELECT count(*) FROM soil_field_summary s WHERE s.land_id = p.land_id),
       (SELECT count(*) FROM weather_daily w WHERE w.land_id = p.land_id)
FROM land_parcels p
LEFT JOIN farms fm ON fm.id = p.farm_id
WHERE {' AND '.join(where)}
ORDER BY p.land_id;
"""
    out = psql(sql)
    if not out:
        print("No matching canonical land parcels.")
        return 0

    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        rows.append(
            {
                "land_id": parts[0],
                "land_name": parts[1],
                "farm_name": parts[2],
                "soil_n": int(parts[3]),
                "weather_n": int(parts[4]),
            }
        )

    print(f"Found {len(rows)} canonical land parcel(s). apply={args.apply}")
    for row in rows:
        actions = []
        if row["soil_n"] == 0:
            actions.append("fetch_soil")
        if row["weather_n"] == 0:
            actions.append("backfill_weather")
        print(
            f"  {row['land_name'] or '(unnamed)'} land_id={row['land_id']} "
            f"farm={row['farm_name'] or '(unassigned)'} soil={row['soil_n']} "
            f"weather={row['weather_n']} -> {actions or ['ok']}"
        )
        if not args.apply:
            continue
        for action in actions:
            task = "soil" if action == "fetch_soil" else "weather"
            celery_delay(task, row["land_id"])
            print(f"    enqueued {task} for land_id={row['land_id']}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or exc.stdout or exc, file=sys.stderr)
        raise SystemExit(1)
