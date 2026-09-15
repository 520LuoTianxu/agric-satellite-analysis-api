"""Claim-agent payloads carry the canonical land_id directly."""

from __future__ import annotations

from app.work_agent import _payload_parts


def test_payload_parts_do_not_translate_identity() -> None:
    land_id, extras = _payload_parts(
        {
            "payload_json": {
                "land_id": "25107",
                "extras": {"days": 30},
            }
        }
    )

    assert land_id == "25107"
    assert extras == {"days": 30}
