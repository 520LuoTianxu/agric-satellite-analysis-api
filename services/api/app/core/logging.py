"""Re-export the shared structured logger used by the API."""

from agric_satellite_analysis_common.logging import logger, setup_logging

__all__ = ["logger", "setup_logging"]
