"""CLI entry point for the API-machine MySQL land synchronizer."""

from __future__ import annotations

import argparse
import asyncio

from app.services.mysql_land_sync import run_land_sync, run_scheduler


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync agricultural lands from MySQL")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one sync and exit")
    mode.add_argument("--loop", action="store_true", help="run daily at 23:00 Asia/Shanghai")
    args = parser.parse_args()
    asyncio.run(run_scheduler() if args.loop else run_land_sync())


if __name__ == "__main__":
    main()

