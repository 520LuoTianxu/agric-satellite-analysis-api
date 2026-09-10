"""Re-export structured logger from openfarm_common."""

from openfarm_common.logging import logger, setup_logging

__all__ = ["logger", "setup_logging"]
