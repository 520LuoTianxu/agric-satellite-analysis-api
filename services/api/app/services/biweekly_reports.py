"""Unregistered API-side factory. Default OFF and deny-all.

Do NOT bind to the current anonymous auth dependencies. Real integration needs
verified tenant JWT + field ownership and production repository/source adapters.
No live router, production query, timer or worker dispatch is introduced here.
"""

from agric_satellite_analysis_common.biweekly.application import BiweeklyService


def build_biweekly_service(
    repository, source, artifacts, *, authorizer=None, offline_enabled=False
):
    return BiweeklyService(
        repository,
        source,
        artifacts,
        authorizer=authorizer,
        offline_enabled=offline_enabled,
    )
