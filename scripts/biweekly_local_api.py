#!/usr/bin/env python3
"""Deprecated. This script is not the biweekly trigger entry.

Triggers must go through the API machine (services/api). Start the API
service locally instead — see /workspace/reports/biweekly-persistent-feature/README.md

    cd services/api
    export PYTHONPATH=.:../../packages/agric_satellite_analysis_common
    export BIWEEKLY_API_ENABLED=1
    export BIWEEKLY_DEV_AUTH=1
    export BIWEEKLY_JWT_SECRET='local-dev-secret-do-not-use-in-prod'
    export BIWEEKLY_ALLOWED_LANDS=6417
    uvicorn app.biweekly_dev_app:app --host 127.0.0.1 --port 8000

Or hang the full API process with app.main:app (needs Redis). Leave
BIWEEKLY_DEV_AUTH off in any environment that is not a local hang.
不能原样上生产.
"""

from __future__ import annotations

import sys

MESSAGE = """ERROR: scripts/biweekly_local_api.py is not the trigger entry.

Start the API-machine service instead:

  cd services/api
  export PYTHONPATH=.:../../packages/agric_satellite_analysis_common
  export BIWEEKLY_API_ENABLED=1
  export BIWEEKLY_DEV_AUTH=1
  export BIWEEKLY_JWT_SECRET='local-dev-secret-do-not-use-in-prod'
  export BIWEEKLY_ALLOWED_LANDS=6417
  export BIWEEKLY_PACK_ROOT=/workspace/reports/field6417-2025-2026
  export BIWEEKLY_WORK_DIR=/tmp/biweekly-api-jobs
  uvicorn app.biweekly_dev_app:app --host 127.0.0.1 --port 8000

Mint a local JWT (same env):

  python -c "from app.middleware.biweekly_auth import mint_dev_jwt; print(mint_dev_jwt(subject='local-dev', tenant_id='dev', land_ids=['6417']))"

Then call /v1/lands/6417/biweekly/... with Authorization: Bearer <jwt>.
See /workspace/reports/biweekly-persistent-feature/README.md
"""


def main(argv=None) -> int:
    sys.stderr.write(MESSAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
