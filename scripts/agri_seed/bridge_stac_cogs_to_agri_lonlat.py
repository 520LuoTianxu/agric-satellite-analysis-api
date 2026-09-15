#!/usr/bin/env python3
"""CLI: bridge STAC/Celery COGs → agric_satellite.parcel_scene_products lonlat_v1.

Implementation lives in ``services/ingest/app/tasks/bridge_stac_cogs_to_agri_lonlat.py``
and accepts the canonical ``land_id`` directly.

Usage (api container — preferred):

    docker compose exec -T ingest python -c \\
      "from app.tasks.bridge_stac_cogs_to_agri_lonlat import main; raise SystemExit(main([\\
        '--land-id','18979']))"

Or from repo root (adds services/api to PYTHONPATH):

    PYTHONPATH=services/ingest python3 scripts/agri_seed/bridge_stac_cogs_to_agri_lonlat.py \\
      --land-id 18979
"""

from __future__ import annotations

import sys
from pathlib import Path

_INGEST_ROOT = Path(__file__).resolve().parents[2] / "services" / "ingest"
if _INGEST_ROOT.is_dir() and str(_INGEST_ROOT) not in sys.path:
    sys.path.insert(0, str(_INGEST_ROOT))

from app.tasks.bridge_stac_cogs_to_agri_lonlat import (  # noqa: E402
    bridge_land_stac_to_agri,
    main,
)

__all__ = ["bridge_land_stac_to_agri", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
