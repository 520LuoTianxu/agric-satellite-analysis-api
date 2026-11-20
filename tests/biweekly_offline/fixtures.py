"""Synthetic inputs only; not a production parcel or calibrated remote-sensing model."""

import copy


def source():
    return copy.deepcopy(
        {
            "tenant_id": "mock-tenant-a",
            "field_id": "MOCK-LAND-001",
            "season_id": "mock-wheat-2027",
            "geometry_fingerprint": "mock-geometry-v1",
            "processing_versions": {
                "mask": "mock-mask-v1",
                "grid": "mock-grid-v1",
                "aggregation": "mock-mean-v1",
                "calibration": "none",
            },
            "crop": "winter_wheat",
            "data_kind": "mock",
            "source_revision": "synthetic-input-v1",
            "window": {"start": "2027-03-15", "end": "2027-03-29"},
            "plan_version": "mock-btu-plan-v1",
            "asset_ids": ["mock-scene-a", "mock-weather-a"],
            "stages": [
                {
                    "period_id": "rising",
                    "stage_key": "rising",
                    "name": "起身期",
                    "start": "2027-03-15",
                    "end": "2027-03-21",
                },
                {
                    "period_id": "jointing",
                    "stage_key": "jointing",
                    "name": "拔节期",
                    "start": "2027-03-21",
                    "end": "2027-04-26",
                },
            ],
            "sampling": {"valid_days": 3, "valid_fraction": 0.87, "max_gap_days": 5},
            "evidence": [
                {
                    "id": "rs.ndvi",
                    "metric": "ndvi",
                    "value": 0.64,
                    "unit": "1",
                    "source_id": "mock-scene-a",
                    "status": "observed",
                    "method_version": "mock-mean-v1",
                    "start": "2027-03-15",
                    "end": "2027-03-29",
                },
                {
                    "id": "rs.ndre",
                    "metric": "ndre",
                    "value": 0.33,
                    "unit": "1",
                    "source_id": "mock-scene-a",
                    "status": "observed",
                    "method_version": "mock-mean-v1",
                    "start": "2027-03-15",
                    "end": "2027-03-29",
                },
                {
                    "id": "weather.rain",
                    "metric": "rain_mm",
                    "value": 8,
                    "unit": "mm",
                    "source_id": "mock-weather-a",
                    "status": "observed",
                    "method_version": "mock-sum-v1",
                    "start": "2027-03-15",
                    "end": "2027-03-29",
                },
                {
                    "id": "rs.fvc",
                    "metric": "fvc",
                    "value": 0.72,
                    "unit": "1",
                    "source_id": "mock-scene-a",
                    "status": "estimated",
                    "method_version": "not-calibrated",
                    "start": "2027-03-15",
                    "end": "2027-03-29",
                },
            ],
        }
    )


def narrative():
    return {
        "summary": "当前仅作计划阶段下的证据说明，建议结合田间记录核查。",
        "claims": [
            {
                "text": "建议核查该阶段冠层表现与田间记录是否相符。",
                "evidence_refs": ["rs.ndvi"],
                "values": [{"evidence_id": "rs.ndvi", "value": 0.64, "unit": "1"}],
            }
        ],
    }
