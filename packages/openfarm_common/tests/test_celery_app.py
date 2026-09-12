"""Unit tests for shared Celery Redis broker transport options."""

from __future__ import annotations

import socket
import unittest

from openfarm_common.celery_app import (
    TASK_ROUTES,
    celery_app_config,
    celery_redis_transport_options,
    create_celery_app,
    redis_socket_keepalive_options,
)
from openfarm_common.settings import CommonSettings


class CeleryRedisTransportTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
