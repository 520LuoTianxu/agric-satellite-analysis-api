"""使用隔离的测试 PostgreSQL 验证洪涝分类读取场景校准元数据。"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from unittest import TestCase

from app.services.agri_alerts import _load_sar_rows


API_ROOT = Path(__file__).resolve().parents[3]
TEST_ENV_FILE = API_ROOT / "ABflow" / ".env.test"


def _test_database_url() -> str:
    """只连接明确配置的测试库，不回退到应用默认数据库。"""
    value = os.environ.get("AGRI_TEST_DATABASE_URL") or dotenv_values(TEST_ENV_FILE).get(
        "DATABASE_URL"
    )
    if not value:
        raise RuntimeError(
            "需要 AGRI_TEST_DATABASE_URL 或 API 仓库 ABflow/.env.test 中的 DATABASE_URL"
        )
    parsed = make_url(value)
    if parsed.drivername != "postgresql+asyncpg":
        raise RuntimeError("遥感元数据集成测试只允许使用 postgresql+asyncpg 测试库")
    return parsed.set(drivername="postgresql+psycopg2").render_as_string(
        hide_password=False
    )


class AgriFloodMetadataDatabaseTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(_test_database_url(), pool_pre_ping=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def test_sar_loader_returns_platform_and_calibration_context(self) -> None:
        land_id = f"codex-flood-metadata-{uuid4().hex}"
        tile_id = f"test-tile-{uuid4().hex}"
        scene_id = "S1D_IW_GRDH_1SDV_20260625T120000_20260625T120025_004762_008EBC_stac"
        pixel_data = {
            "format": "lonlat_v1",
            "relative_orbit": 171,
            "stac_item_id": scene_id.removesuffix("_stac"),
            "radiometric_calibration": {
                "method": "esa_sigma_nought_lut",
                "platform": "S1D",
                "processing_version": "004.03",
                "calibration_epoch": "not_applicable",
                "acquisition_datetime": "2026-06-25T12:00:00Z",
            },
        }

        with self.engine.connect() as connection:
            transaction = connection.begin()
            try:
                boundary = {
                    "type": "Polygon",
                    "coordinates": [
                        [[121.0, 31.0], [121.001, 31.0], [121.001, 31.001], [121.0, 31.001], [121.0, 31.0]]
                    ],
                }
                connection.execute(
                    text(
                        """
                        INSERT INTO agric_satellite.land_parcels (
                            land_id, tile_id, boundary_geojson,
                            min_lon, min_lat, max_lon, max_lat,
                            source_file, source_feature_index
                        ) VALUES (
                            :land_id, :tile_id, CAST(:boundary AS jsonb),
                            121.0, 31.0, 121.001, 31.001,
                            'codex-flood-metadata-integration', 0
                        )
                        """
                    ),
                    {
                        "land_id": land_id,
                        "tile_id": tile_id,
                        "boundary": json.dumps(boundary),
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO agric_satellite.parcel_scene_products (
                            land_id, tile_id, date, sensor, scene_id,
                            pixel_data_url, pixel_data, vv_avg, vh_avg
                        ) VALUES (
                            :land_id, :tile_id, :scene_date, 'S1', :scene_id,
                            '', CAST(:pixel_data AS jsonb), -18.0, -23.0
                        )
                        """
                    ),
                    {
                        "land_id": land_id,
                        "tile_id": tile_id,
                        "scene_date": date(2026, 6, 25),
                        "scene_id": scene_id,
                        "pixel_data": json.dumps(pixel_data),
                    },
                )

                rows = _load_sar_rows(connection, land_id, date(2026, 6, 25))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["platform"], "S1D")
                self.assertEqual(rows[0]["processing_version"], "004.03")
                self.assertEqual(rows[0]["calibration_epoch"], "not_applicable")
                self.assertEqual(
                    rows[0]["acquisition_datetime"], "2026-06-25T12:00:00Z"
                )
                self.assertEqual(rows[0]["calibration_method"], "esa_sigma_nought_lut")
                self.assertEqual(rows[0]["relative_orbit"], "171")
                self.assertEqual(rows[0]["stac_item_id"], scene_id.removesuffix("_stac"))
            finally:
                transaction.rollback()

        with self.engine.connect() as verify:
            remaining = verify.execute(
                text(
                    """
                    SELECT
                        (SELECT count(*) FROM agric_satellite.land_parcels WHERE land_id = :land_id)
                        + (SELECT count(*) FROM agric_satellite.parcel_scene_products WHERE land_id = :land_id)
                    """
                ),
                {"land_id": land_id},
            ).scalar_one()
            verify.rollback()
        if remaining:
            # 仅按本测试的随机地块ID清理，避免影响共享测试库中的其他数据。
            with self.engine.begin() as cleanup:
                cleanup.execute(
                    text(
                        "DELETE FROM agric_satellite.parcel_scene_products "
                        "WHERE land_id = :land_id"
                    ),
                    {"land_id": land_id},
                )
                cleanup.execute(
                    text(
                        "DELETE FROM agric_satellite.land_parcels "
                        "WHERE land_id = :land_id"
                    ),
                    {"land_id": land_id},
                )
            with self.engine.connect() as verify_after_cleanup:
                remaining = verify_after_cleanup.execute(
                    text(
                        """
                        SELECT
                            (SELECT count(*) FROM agric_satellite.land_parcels WHERE land_id = :land_id)
                            + (SELECT count(*) FROM agric_satellite.parcel_scene_products WHERE land_id = :land_id)
                        """
                    ),
                    {"land_id": land_id},
                ).scalar_one()
                verify_after_cleanup.rollback()
        self.assertEqual(int(remaining), 0, "测试结束后仍存在本测试创建的数据")


if __name__ == "__main__":
    import unittest

    unittest.main()
