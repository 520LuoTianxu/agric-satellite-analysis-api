#!/usr/bin/env python3
"""Smoke: publish one TaskMessage to CloudAMQP task queue.

Usage (from repo root, with .env loaded or env exported):

  pip install -e packages/openfarm_common
  python scripts/mq_publish_test.py --field-id <uuid>
  python scripts/mq_publish_test.py --parcel-id <land_id> --mode bridge_only
  python scripts/mq_publish_test.py --field-id <uuid> --type weather_backfill --days 30
  python scripts/mq_publish_test.py --field-id <uuid> --type soil_fetch
  python scripts/mq_publish_test.py --field-id <uuid> --type field_bootstrap

Never prints CLOUDAMQP password (uses connection_label).
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

# Load .env if present (no python-dotenv dependency)
def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def main() -> int:
    _load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--field-id", default=None)
    ap.add_argument("--parcel-id", default=None, help="agri land_id")
    ap.add_argument("--land-id", default=None)
    ap.add_argument(
        "--type",
        default="satellite_analysis",
        choices=(
            "satellite_analysis",
            "agri_bridge",
            "weather_backfill",
            "soil_fetch",
            "field_bootstrap",
        ),
    )
    ap.add_argument("--mode", default="full", choices=("full", "bridge_only"))
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--days", type=int, default=None, help="weather_backfill days")
    ap.add_argument("--skip-indices", action="store_true", help="field_bootstrap")
    ap.add_argument("--task-id", default=None)
    args = ap.parse_args()

    if not args.field_id and not args.parcel_id and not args.land_id:
        ap.error("provide --field-id and/or --parcel-id/--land-id")

    # Ensure repo packages importable when not installed
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "packages" / "openfarm_common"))

    from openfarm_common.mq import connection_label, publish_task
    from openfarm_common.mq_schemas import TaskMessage
    from openfarm_common.settings import settings

    if not settings.cloudamqp_url:
        print("ERROR: CLOUDAMQP_URL not set", file=sys.stderr)
        return 1

    task_id = args.task_id or str(uuid.uuid4())
    extras: dict = {"smoke": True}
    if args.type in ("satellite_analysis", "agri_bridge"):
        extras["mode"] = args.mode
        extras["months"] = args.months
    if args.type == "weather_backfill" and args.days is not None:
        extras["days"] = args.days
    if args.type == "field_bootstrap" and args.skip_indices:
        extras["skip_indices"] = True

    msg = TaskMessage(
        task_id=task_id,
        type=args.type,
        field_id=args.field_id,
        parcel_id=args.parcel_id,
        land_id=args.land_id,
        extras=extras,
    )
    print(f"broker={connection_label()}")
    print(f"queue={settings.cloudamqp_download_queue}")
    print(f"publishing task_id={task_id} type={msg.type}")
    publish_task(msg)
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
