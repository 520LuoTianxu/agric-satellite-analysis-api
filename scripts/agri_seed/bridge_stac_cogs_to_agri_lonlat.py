#!/usr/bin/env python3
"""CLI: bridge STAC/Celery COGs → agri.parcel_scene_products lonlat_v1.

Implementation lives in ``services/api/app/tasks/bridge_stac_cogs_to_agri_lonlat.py``
so Celery can import ``bridge_field_stac_to_agri``.

Usage (api container — preferred):

    docker compose exec -T api python -c \\
      "from app.tasks.bridge_stac_cogs_to_agri_lonlat import main; raise SystemExit(main([\\
        '--field-id','0fa5ecc0-944b-4202-a0f8-1ba78ae3746c']))"

Or from repo root (adds services/api to PYTHONPATH):

    PYTHONPATH=services/api python3 scripts/agri_seed/bridge_stac_cogs_to_agri_lonlat.py \\
      --field-id 0fa5ecc0-944b-4202-a0f8-1ba78ae3746c --land-id 18979
"""

from __future__ import annotations

import sys
from pathlib import Path

_API_ROOT = Path(__file__).resolve().parents[2] / "services" / "api"
if _API_ROOT.is_dir() and str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from app.tasks.bridge_stac_cogs_to_agri_lonlat import (  # noqa: E402
    bridge_field_stac_to_agri,
    main,
)

__all__ = ["bridge_field_stac_to_agri", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
