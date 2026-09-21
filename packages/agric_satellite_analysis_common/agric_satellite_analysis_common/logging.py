"""Structured logging for workers."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import TextIO

import structlog


_RUNTIME_LOG_STREAM: TextIO | None = None
_RUNTIME_LOG_FILE: TextIO | None = None


class _TeeStream:
    """同时写 stdout 和文件，避免落盘后失去容器日志采集。"""

    def __init__(self, primary: TextIO, mirror: TextIO) -> None:
        self._primary = primary
        self._mirror: TextIO | None = mirror

    def write(self, text: str) -> int:
        written = self._primary.write(text)
        try:
            if self._mirror is not None:
                self._mirror.write(text)
                self._mirror.flush()
        except OSError:
            # 日志目录不可写或磁盘暂满时不能影响业务任务；保留 stdout 作为兜底。
            self._mirror = None
        return written

    def flush(self) -> None:
        self._primary.flush()
        try:
            if self._mirror is not None:
                self._mirror.flush()
        except OSError:
            self._mirror = None

    def isatty(self) -> bool:
        return self._primary.isatty()


def _runtime_log_stream() -> TextIO:
    """获取后端统一运行日志输出流，文件路径由 OPENFARM_LOG_FILE 指定。"""

    global _RUNTIME_LOG_STREAM, _RUNTIME_LOG_FILE
    if _RUNTIME_LOG_STREAM is not None:
        return _RUNTIME_LOG_STREAM

    log_file = (os.getenv("OPENFARM_LOG_FILE") or "").strip()
    if not log_file:
        _RUNTIME_LOG_STREAM = sys.stdout
        return _RUNTIME_LOG_STREAM

    try:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _RUNTIME_LOG_FILE = log_path.open("a", encoding="utf-8", buffering=1)
    except OSError as exc:
        # 日志落盘失败时仍允许服务启动，并把原因留在容器 stderr 中。
        print(f"[openfarm] 无法打开运行日志文件 {log_file}: {exc}", file=sys.stderr)
        _RUNTIME_LOG_STREAM = sys.stdout
        return _RUNTIME_LOG_STREAM

    _RUNTIME_LOG_STREAM = _TeeStream(sys.stdout, _RUNTIME_LOG_FILE)
    return _RUNTIME_LOG_STREAM


def _attach_runtime_file_handler() -> None:
    """把标准 logging 记录追加到统一文件，覆盖 API/Celery 的非 structlog 日志。"""

    if _RUNTIME_LOG_FILE is None:
        return

    root_logger = logging.getLogger()
    if any(handler.get_name() == "openfarm.runtime.file" for handler in root_logger.handlers):
        return

    handler = logging.StreamHandler(_RUNTIME_LOG_FILE)
    handler.set_name("openfarm.runtime.file")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s trace_id=%(trace_id)s %(message)s")
    )
    root_logger.addHandler(handler)
    if root_logger.level > logging.INFO:
        root_logger.setLevel(logging.INFO)


def setup_stdlib_logging() -> None:
    """配置使用标准 logging 的进程，使其也写入统一运行日志文件。"""

    from agric_satellite_analysis_common.trace import install_stdlib_trace_log_record

    install_stdlib_trace_log_record()
    root_logger = logging.getLogger()
    had_handlers = bool(root_logger.handlers)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s trace_id=%(trace_id)s %(message)s",
        stream=_runtime_log_stream(),
    )
    # 没有既有 handler 时 basicConfig 已使用 TeeStream；只有 basicConfig 被外部配置跳过时才补文件 handler。
    if had_handlers:
        _attach_runtime_file_handler()


def setup_logging() -> None:
    from agric_satellite_analysis_common.trace import install_stdlib_trace_log_record

    install_stdlib_trace_log_record()
    _runtime_log_stream()
    _attach_runtime_file_handler()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=_runtime_log_stream()),
        cache_logger_on_first_use=True,
    )


logger = structlog.get_logger("openfarm")
