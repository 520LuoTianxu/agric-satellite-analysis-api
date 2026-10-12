"""事件循环卡顿看门狗 + 独立线程健康端口。

背景：生产 API 是单进程 uvicorn。/health 与业务路由共用同一个事件循环，任何在
事件循环线程里的同步阻塞（大 JSON 解析、纯 CPU 计算、同步 DB 调用）都会让
istio/k8s 的 livez/readyz 等不到响应头，连续超时后 Pod 被重启，日志里只剩最后
一条普通请求（如 task-runs status 409），看不出是谁卡住的。

本模块：
1. 心跳协程每 ``interval`` 秒在事件循环里打点；
2. 看门狗线程发现心跳超过 ``stall_log_seconds`` 未更新时，打印事件循环线程的
   当前 Python 栈（限频），直接给出“谁在阻塞”的生产证据；
3. 可选的独立 HTTP 线程（``HEALTH_THREAD_PORT``）提供 /livez、/readyz：
   不经过事件循环，/livez 只有在循环卡住超过 ``livez_max_stall_seconds`` 才失败，
   /readyz 在卡住超过 ``readyz_max_stall_seconds`` 时失败（摘流量但不重启）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.core.logging import logger


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


class LoopWatchdog:
    def __init__(
        self,
        *,
        interval: float = 1.0,
        stall_log_seconds: float | None = None,
        livez_max_stall_seconds: float | None = None,
        readyz_max_stall_seconds: float | None = None,
    ) -> None:
        self.interval = interval
        self.stall_log_seconds = stall_log_seconds or _env_float("LOOP_STALL_LOG_SECONDS", 5.0)
        self.livez_max_stall_seconds = livez_max_stall_seconds or _env_float(
            "LIVEZ_MAX_LOOP_STALL_SECONDS", 180.0
        )
        self.readyz_max_stall_seconds = readyz_max_stall_seconds or _env_float(
            "READYZ_MAX_LOOP_STALL_SECONDS", 15.0
        )
        self.last_beat = time.monotonic()
        self.loop_thread_id: int | None = None
        self._stop = threading.Event()
        self._task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None
        self._server: ThreadingHTTPServer | None = None
        self._last_stack_log = 0.0

    # ── 状态 ──────────────────────────────────────────────
    def stall_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.last_beat)

    def live(self) -> bool:
        return self.stall_seconds() < self.livez_max_stall_seconds

    def ready(self) -> bool:
        return self.stall_seconds() < self.readyz_max_stall_seconds

    # ── 运行 ──────────────────────────────────────────────
    async def _beat(self) -> None:
        while True:
            self.last_beat = time.monotonic()
            await asyncio.sleep(self.interval)

    def loop_stack(self) -> str:
        frame = sys._current_frames().get(self.loop_thread_id or -1)
        if frame is None:
            return ""
        return "".join(traceback.format_stack(frame)[-25:])

    def _watch(self) -> None:
        while not self._stop.wait(self.interval):
            stall = self.stall_seconds()
            now = time.monotonic()
            if stall >= self.stall_log_seconds and now - self._last_stack_log >= 10:
                self._last_stack_log = now
                logger.warning(
                    "event_loop_stalled",
                    stall_seconds=round(stall, 1),
                    loop_stack=self.loop_stack(),
                )

    def start(self, port: int | None = None) -> None:
        self.loop_thread_id = threading.get_ident()
        self.last_beat = time.monotonic()
        self._task = asyncio.get_running_loop().create_task(self._beat(), name="loop-watchdog-beat")
        self._thread = threading.Thread(target=self._watch, name="loop-watchdog", daemon=True)
        self._thread.start()
        if port:
            self._server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(self))
            self._server.daemon_threads = True
            threading.Thread(
                target=self._server.serve_forever, name="health-thread-server", daemon=True
            ).start()
            logger.info("health_thread_server_started", port=port)

    async def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


def _make_handler(dog: LoopWatchdog):
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/")
            if path in ("/livez", "/health/livez"):
                ok = dog.live()
            elif path in ("/readyz", "/health/readyz"):
                ok = dog.ready()
            else:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(
                {"status": "ok" if ok else "stalled", "loop_stall_seconds": round(dog.stall_seconds(), 1)}
            ).encode()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:  # 探针高频，不刷访问日志
            return

    return _Handler


watchdog = LoopWatchdog()
