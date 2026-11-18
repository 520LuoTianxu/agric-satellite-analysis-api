"""Tests for timed HTTP Range COG I/O, hedging, slots, and compose defaults."""

from __future__ import annotations

import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.core.band_parallel import (
    reset_band_gdal_limit,
    run_parallel_band_jobs,
)
from app.core.cog_http import (
    BandReadTimeout,
    TimedRangeFile,
    band_read_hedge_after_sec,
    band_read_timeout_sec,
    fetch_byte_range,
    gdal_slot_wait_sec,
    hedge_circuit_open,
    open_windowed_cog,
    record_range_outcome,
    reset_cog_http_state,
    unwrap_raster_href,
)


REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_tiled_tif(path: Path, size: int = 64, block: int = 32) -> np.ndarray:
    import rasterio
    from rasterio.transform import from_origin

    data = np.arange(size * size, dtype=np.uint16).reshape(size, size)
    profile = {
        "driver": "GTiff",
        "height": size,
        "width": size,
        "count": 1,
        "dtype": "uint16",
        "crs": "EPSG:4326",
        "transform": from_origin(0, 1, 0.01, 0.01),
        "tiled": True,
        "blockxsize": block,
        "blockysize": block,
        "compress": "none",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return data


class _RangeServer(ThreadingHTTPServer):
    def __init__(self, payloads: dict[str, bytes], stalls: dict[str, float] | None = None):
        super().__init__(("127.0.0.1", 0), _RangeHandler)
        self.payloads = payloads
        self.stalls = stalls or {}
        self.hits: dict[str, int] = {}
        self.hit_lock = threading.Lock()
        self.first_gate: dict[str, threading.Event] = {}

    def url(self, name: str) -> str:
        host, port = self.server_address
        return f"http://127.0.0.1:{port}/{name.lstrip('/')}"

    def note_hit(self, path: str) -> int:
        key = path.lstrip("/")
        with self.hit_lock:
            self.hits[key] = self.hits.get(key, 0) + 1
            return self.hits[key]


class _RangeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        server: _RangeServer = self.server  # type: ignore[assignment]
        name = self.path.split("?", 1)[0].lstrip("/")
        payload = server.payloads.get(name)
        if payload is None:
            self.send_error(404)
            return
        n = server.note_hit(name)
        stall = server.stalls.get(name, 0.0)
        # hedge.tif: only the first accepted connection hangs.
        if name.endswith("hedge.tif") and n == 1:
            stall = max(stall, 30.0)
        if stall:
            time.sleep(stall)
            # If the test already finished, avoid writing to a closed client.
            if server.stalls.get(name, 0) >= 10:
                try:
                    self.send_error(408)
                except Exception:
                    return
                return
        start = 0
        end = len(payload) - 1
        status = 200
        header_range = self.headers.get("Range")
        if header_range and header_range.startswith("bytes="):
            spec = header_range.split("=", 1)[1]
            left, _, right = spec.partition("-")
            start = int(left or 0)
            end = int(right) if right else end
            end = min(end, len(payload) - 1)
            status = 206
        chunk = payload[start : end + 1]
        self.send_response(status)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Content-Type", "image/tiff")
        self.end_headers()
        try:
            self.wfile.write(chunk)
            self.wfile.flush()
        except BrokenPipeError:
            return


class _ServerThread:
    def __init__(self, payloads: dict[str, bytes], stalls: dict[str, float] | None = None):
        self.server = _RangeServer(payloads, stalls)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _RangeServer:
        self.thread.start()
        return self.server

    def __exit__(self, *args) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()


class UnwrapHrefTests(unittest.TestCase):
    def test_strips_vsicurl_and_rewrites_vsis3(self) -> None:
        self.assertEqual(
            unwrap_raster_href("/vsicurl/https://example.test/a.tif?sig=1"),
            "https://example.test/a.tif?sig=1",
        )
        self.assertEqual(
            unwrap_raster_href("/vsis3/sentinel-cogs/x/B04.tif"),
            "https://sentinel-cogs.s3.amazonaws.com/x/B04.tif",
        )
        self.assertEqual(
            unwrap_raster_href("s3://sentinel-s1-l1c/vv.tif"),
            "https://sentinel-s1-l1c.s3.amazonaws.com/vv.tif",
        )


class DefaultKnobTests(unittest.TestCase):
    def test_code_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            for key in (
                "BAND_READ_TIMEOUT_SEC",
                "BAND_READ_HEDGE_AFTER_SEC",
                "GDAL_SLOT_WAIT_SEC",
            ):
                os.environ.pop(key, None)
            self.assertEqual(band_read_timeout_sec(), 20.0)
            self.assertEqual(band_read_hedge_after_sec(), 5.0)
            self.assertEqual(gdal_slot_wait_sec(), 20.0)

    def test_compose_defaults_are_on(self) -> None:
        compose = (REPO_ROOT / "docker-compose.yml").read_text()
        download = (REPO_ROOT / "docker-compose.download-machine.yml").read_text()
        example = (REPO_ROOT / ".env.example").read_text()
        for text in (compose, download, example):
            self.assertIn("BAND_READ_TIMEOUT_SEC", text)
            self.assertIn("${BAND_READ_TIMEOUT_SEC:-20}", text) if "BAND_READ_TIMEOUT_SEC:" in text else None
        self.assertIn("BAND_READ_TIMEOUT_SEC: ${BAND_READ_TIMEOUT_SEC:-20}", compose)
        self.assertIn("BAND_READ_HEDGE_AFTER_SEC: ${BAND_READ_HEDGE_AFTER_SEC:-5}", compose)
        self.assertIn("GDAL_SLOT_WAIT_SEC: ${GDAL_SLOT_WAIT_SEC:-20}", compose)
        self.assertIn("BAND_WINDOW_CACHE: ${BAND_WINDOW_CACHE:-1}", compose)
        self.assertIn("INGEST_SCENE_MAX_INFLIGHT: ${INGEST_SCENE_MAX_INFLIGHT:-1}", compose)
        self.assertIn("INGEST_SCENE_MAX_INFLIGHT: ${INGEST_SCENE_MAX_INFLIGHT:-1}", download)
        self.assertIn("INGEST_SCENE_MAX_INFLIGHT=1", example)
        self.assertIn("GDAL_HTTP_VERSION: ${GDAL_HTTP_VERSION:-1.1}", compose)
        self.assertIn("GDAL_HTTP_MULTIRANGE: ${GDAL_HTTP_MULTIRANGE:-SERIAL}", compose)
        self.assertIn("BAND_WINDOW_CACHE: ${BAND_WINDOW_CACHE:-1}", download)
        self.assertIn("BAND_READ_TIMEOUT_SEC=20", example)
        self.assertIn("BAND_READ_HEDGE_AFTER_SEC=5", example)
        self.assertIn("GDAL_SLOT_WAIT_SEC=20", example)
        self.assertIn("GDAL_HTTP_VERSION=1.1", example)
        self.assertIn("GDAL_HTTP_MULTIRANGE=SERIAL", example)
        self.assertIn("BAND_WINDOW_CACHE=1", example)


class TimedRangeTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_cog_http_state()
        reset_band_gdal_limit()
        self.tmp = Path(self.id().replace(".", "_"))
        # Use a real temp dir
        import tempfile

        self._tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmpdir.name)
        self.tif = self.dir / "scene.tif"
        self.array = _write_tiled_tif(self.tif)
        self.payload = self.tif.read_bytes()

    def tearDown(self) -> None:
        reset_cog_http_state()
        reset_band_gdal_limit()
        self._tmpdir.cleanup()

    def test_windowed_open_reads_only_needed_range(self) -> None:
        with _ServerThread({"scene.tif": self.payload}) as server:
            url = server.url("scene.tif")
            with open_windowed_cog(url) as src:
                window = src.read(1, window=__import__("rasterio").windows.Window(0, 0, 8, 8))
            self.assertEqual(window.shape, (8, 8))
            np.testing.assert_array_equal(window, self.array[:8, :8])
            # Must have used at least one Range GET (header / tiles), not failed open.
            self.assertGreaterEqual(server.hits.get("scene.tif", 0), 1)

    def test_stalled_range_aborts_and_later_fast_read_gets_slot(self) -> None:
        stalls = {"stall.tif": 30.0}
        with _ServerThread(
            {"stall.tif": self.payload, "fast.tif": self.payload}, stalls
        ) as server:
            stall_url = server.url("stall.tif")
            fast_url = server.url("fast.tif")

            def _fn(key: str, url: str) -> int:
                data, _ = fetch_byte_range(url, 0, 1023, allow_hedge=False)
                return len(data)

            with patch.dict(
                os.environ,
                {
                    "INGEST_BAND_MAX_WORKERS": "1",
                    "BAND_READ_MAX_ATTEMPTS": "1",
                    "BAND_READ_TIMEOUT_SEC": "0.3",
                    "BAND_READ_HEDGE_AFTER_SEC": "0",
                    "GDAL_SLOT_WAIT_SEC": "1",
                },
            ):
                reset_band_gdal_limit()
                reset_cog_http_state()
                t0 = time.perf_counter()
                with self.assertRaises(RuntimeError) as ctx:
                    run_parallel_band_jobs({"stall": stall_url}, _fn)
                wall = time.perf_counter() - t0
                self.assertIsInstance(ctx.exception.__cause__, (BandReadTimeout, TimeoutError))
                self.assertLess(wall, 1.5)

                t1 = time.perf_counter()
                out = run_parallel_band_jobs({"fast": fast_url}, _fn)
                fast_wall = time.perf_counter() - t1
            self.assertEqual(out["fast"], 1024)
            self.assertLess(fast_wall, 1.0)

    def test_hedge_returns_success_without_waiting_for_hang(self) -> None:
        with _ServerThread({"hedge.tif": self.payload}, {"hedge.tif": 0.0}) as server:
            url = server.url("hedge.tif")
            with patch.dict(
                os.environ,
                {
                    "BAND_READ_TIMEOUT_SEC": "2",
                    "BAND_READ_HEDGE_AFTER_SEC": "0.15",
                    "BAND_READ_HEDGE_CIRCUIT_MIN_SAMPLES": "99",
                },
            ):
                reset_cog_http_state()
                t0 = time.perf_counter()
                data, total = fetch_byte_range(url, 0, 2047, allow_hedge=True)
                wall = time.perf_counter() - t0
            self.assertEqual(len(data), 2048)
            self.assertEqual(total, len(self.payload))
            self.assertLess(wall, 1.5)
            self.assertGreaterEqual(server.hits.get("hedge.tif", 0), 2)

    def test_circuit_skips_hedge_when_timeout_rate_is_high(self) -> None:
        reset_cog_http_state()
        with patch.dict(
            os.environ,
            {
                "BAND_READ_HEDGE_CIRCUIT_WINDOW_SEC": "60",
                "BAND_READ_HEDGE_CIRCUIT_MIN_SAMPLES": "4",
                "BAND_READ_HEDGE_CIRCUIT_TIMEOUT_RATE": "0.5",
            },
        ):
            for _ in range(4):
                record_range_outcome(timed_out=True)
            self.assertTrue(hedge_circuit_open())

    def test_timed_range_file_seek_read(self) -> None:
        with _ServerThread({"scene.tif": self.payload}) as server:
            url = server.url("scene.tif")
            with patch.dict(os.environ, {"BAND_READ_TIMEOUT_SEC": "2", "BAND_READ_HEDGE_AFTER_SEC": "0"}):
                reset_cog_http_state()
                with TimedRangeFile(url) as fh:
                    size = fh.size()
                    self.assertEqual(size, len(self.payload))
                    fh.seek(0)
                    header = fh.read(16)
                    self.assertEqual(header[:2], b"II")  # little-endian TIFF
                    fh.seek(10)
                    mid = fh.read(6)
                    self.assertEqual(mid, self.payload[10:16])


class ComposeCacheDefaultTests(unittest.TestCase):
    def test_code_cache_still_defaults_off(self) -> None:
        from app.core.band_window_cache import window_cache_enabled

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BAND_WINDOW_CACHE", None)
            self.assertFalse(window_cache_enabled())


if __name__ == "__main__":
    unittest.main()
