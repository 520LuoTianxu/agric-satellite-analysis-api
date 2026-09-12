"""Small pagination helpers for list endpoints."""

from __future__ import annotations


def newest_page_offset(total: int, limit: int) -> int:
    """Return OFFSET for an ascending query that yields the newest ``limit`` rows.

    Equivalent client strategy to ``ORDER BY date DESC LIMIT :limit`` then
    reversing the page: ``order=asc&offset=newest_page_offset(total, limit)``.
    """
    if limit <= 0:
        return 0
    return max(0, int(total) - int(limit))
