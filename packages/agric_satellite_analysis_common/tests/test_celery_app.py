"""Unit tests for shared Celery Redis broker transport options."""

from __future__ import annotations

import socket
import os
import unittest
from unittest.mock import patch

from agric_satellite_analysis_common.celery_app import (
    BEAT_SCHEDULE,
    TASK_ROUTES,
    CPU_COMPUTE_QUEUE,
    SATELLITE_DOWNLOAD_QUEUE,
    celery_app_config,
    celery_redis_transport_options,
    create_celery_app,
    enabled_beat_schedule,
    BEAT_SWITCHES,
    redis_socket_keepalive_options,
    task_queue_for,
)
from agric_satellite_analysis_common.settings import CommonSettings


class CeleryRedisTransportTests(unittest.TestCase):
    def test_daily_satellite_schedule_is_1915_china_time(self) -> None:
        schedule = BEAT_SCHEDULE["refresh-satellite-overview-daily"]["schedule"]
        self.assertEqual(schedule.hour, {11})
        self.assertEqual(schedule.minute, {15})

    def test_transport_options_set_timeouts_and_health_check(self) -> None:
        cfg = CommonSettings(
            celery_broker_visibility_timeout=7200,
            celery_redis_socket_timeout=5.0,
            celery_redis_socket_connect_timeout=5.0,
            celery_redis_socket_keepalive=True,
            celery_redis_retry_on_timeout=True,
            celery_redis_health_check_interval=25,
        )
        opts = celery_redis_transport_options(cfg)
        self.assertEqual(opts["visibility_timeout"], 7200)
        self.assertEqual(opts["socket_timeout"], 5.0)
        self.assertEqual(opts["socket_connect_timeout"], 5.0)
        self.assertTrue(opts["socket_keepalive"])
        self.assertTrue(opts["retry_on_timeout"])
        self.assertEqual(opts["health_check_interval"], 25)
        keepalive = opts.get("socket_keepalive_options")
        if keepalive:
            self.assertIsInstance(keepalive, dict)
            self.assertTrue(keepalive)

    def test_keepalive_options_use_os_tcp_flags(self) -> None:
        opts = redis_socket_keepalive_options()
        idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(
            socket, "TCP_KEEPALIVE", None
        )
        if idle is None:
            self.skipTest("this platform has no TCP keepalive idle flag")
        self.assertEqual(opts[int(idle)], 60)
        interval = getattr(socket, "TCP_KEEPINTVL", None)
        if interval is not None:
            self.assertEqual(opts[int(interval)], 10)
        count = getattr(socket, "TCP_KEEPCNT", None)
        if count is not None:
            self.assertEqual(opts[int(count)], 3)

    def test_keepalive_can_be_disabled(self) -> None:
        cfg = CommonSettings(celery_redis_socket_keepalive=False)
        opts = celery_redis_transport_options(cfg)
        self.assertFalse(opts["socket_keepalive"])
        self.assertNotIn("socket_keepalive_options", opts)

    def test_app_config_covers_broker_and_backend(self) -> None:
        cfg = CommonSettings(celery_broker_connection_max_retries=0)
        conf = celery_app_config(cfg)
        self.assertEqual(conf["broker_connection_max_retries"], 0)
        self.assertTrue(conf["broker_connection_retry"])
        self.assertTrue(conf["broker_connection_retry_on_startup"])
        self.assertTrue(conf["broker_channel_error_retry"])
        self.assertTrue(conf["redis_retry_on_timeout"])
        self.assertTrue(conf["redis_socket_keepalive"])
        self.assertEqual(conf["redis_socket_timeout"], 5.0)
        self.assertEqual(conf["redis_backend_health_check_interval"], 25)
        self.assertNotIn("visibility_timeout", conf["result_backend_transport_options"])
        self.assertEqual(
            conf["broker_transport_options"]["socket_timeout"],
            conf["result_backend_transport_options"]["socket_timeout"],
        )

    def test_beat_schedule_is_opt_in(self) -> None:
        with_beat = create_celery_app(
            name="openfarm-beat-test",
            include=[],
            default_queue="ingest",
            with_beat_schedule=True,
        )
        without_beat = create_celery_app(
            name="openfarm-nobeat-test",
            include=[],
            default_queue="ingest",
        )
        self.assertFalse(with_beat.conf.beat_schedule)
        self.assertFalse(without_beat.conf.beat_schedule)

    def test_each_schedule_has_an_independent_opt_in_switch(self):
        defaults = {attribute: False for attribute in BEAT_SWITCHES.values()}
        for name, attribute in BEAT_SWITCHES.items():
            cfg = CommonSettings(_env_file=None, **{**defaults, attribute: True})
            self.assertEqual(set(enabled_beat_schedule(cfg)), {name})

    def test_all_switches_can_be_enabled_explicitly(self):
        cfg = CommonSettings(
            _env_file=None, **{attribute: True for attribute in BEAT_SWITCHES.values()}
        )
        self.assertEqual(set(enabled_beat_schedule(cfg)), set(BEAT_SWITCHES))

    def test_env_boolean_values_control_registration(self):
        env = {attribute.upper(): "false" for attribute in BEAT_SWITCHES.values()}
        with patch.dict(os.environ, env):
            self.assertEqual(enabled_beat_schedule(CommonSettings(_env_file=None)), {})
            os.environ["SCHEDULE_DAILY_SATELLITE_ENABLED"] = "true"
            cfg = CommonSettings(_env_file=None)
            self.assertEqual(
                set(enabled_beat_schedule(cfg)), {"refresh-satellite-overview-daily"}
            )
            with patch("agric_satellite_analysis_common.celery_app.settings", cfg):
                app = create_celery_app(name="beat-enabled", with_beat_schedule=True)
            self.assertEqual(
                set(app.conf.beat_schedule), {"refresh-satellite-overview-daily"}
            )

    def test_create_celery_app_inherits_shared_transport(self) -> None:
        expected = celery_app_config()
        app = create_celery_app(
            name="openfarm-test", include=[], default_queue="ingest"
        )
        self.assertEqual(
            dict(app.conf.broker_transport_options),
            expected["broker_transport_options"],
        )
        self.assertTrue(app.conf.broker_connection_retry_on_startup)
        self.assertEqual(
            app.conf.broker_connection_max_retries,
            expected["broker_connection_max_retries"],
        )

    def test_decloud_tasks_route_to_decloud_queue(self) -> None:
        """Download-machine runs UnCRtainTS on -Q decloud, not ingest."""
        self.assertEqual(
            TASK_ROUTES["app.tasks.decloud_uncrtaints.*"]["queue"],
            "decloud",
        )

    def test_business_task_families_are_resource_isolated(self) -> None:
        self.assertEqual(
            TASK_ROUTES["app.tasks.agri_lonlat.*"]["queue"],
            SATELLITE_DOWNLOAD_QUEUE,
        )
        self.assertEqual(
            TASK_ROUTES["app.tasks.sentinel1.*"]["queue"],
            SATELLITE_DOWNLOAD_QUEUE,
        )
        self.assertEqual(
            TASK_ROUTES["app.tasks.satellite_batch.*"]["queue"],
            SATELLITE_DOWNLOAD_QUEUE,
        )
        self.assertEqual(
            TASK_ROUTES["app.tasks.backfill.*"]["queue"],
            CPU_COMPUTE_QUEUE,
        )
        self.assertEqual(
            task_queue_for(
                "app.tasks.overview_preagg.refresh_overview_stats",
                requested_queue="ingest",
            ),
            CPU_COMPUTE_QUEUE,
        )
        self.assertEqual(
            task_queue_for(
                "app.tasks.satellite_batch.process_satellite_batch",
                requested_queue="ingest",
            ),
            SATELLITE_DOWNLOAD_QUEUE,
        )

    def test_weekly_index_is_not_scheduled(self) -> None:
        self.assertNotIn("compute-indices-weekly", BEAT_SCHEDULE)


if __name__ == "__main__":
    unittest.main()
