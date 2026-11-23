"""Structured logging for workers."""

from __future__ import annotations

import logging
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any, TextIO

import structlog

# 异常正文里的口令、云账号和连接串必须先抹掉再进 JSON。
# 只匹配「名字 + 赋值」，避免误伤地块号、sha256 这类普通字段。
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|access_key(?:_id|_secret)?|authorization|ossaccesskeyid|signature)\b"
    r"\s*[=:]\s*[\"']?([^\s,;\"']+)"
)
_URI_RE = re.compile(
    r"(?i)\b(?:postgres(?:ql)?(?:\+\w+)?|redis(?:s)?|mysql|amqp|amqps)://[^\s\"']+"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b")
_ALIYUN_AK_RE = re.compile(r"\bLTAI[A-Za-z0-9]{8,}\b")


def redact_secrets(text: str, *, limit: int = 4000) -> str:
    """Return ``text`` with credentials removed and length capped."""
    cleaned = text or ""
    cleaned = _URI_RE.sub("[uri:redacted]", cleaned)
    cleaned = _BEARER_RE.sub("Bearer [redacted]", cleaned)
    cleaned = _JWT_RE.sub("[jwt:redacted]", cleaned)
    cleaned = _ALIYUN_AK_RE.sub("[access-key:redacted]", cleaned)
    cleaned = _SECRET_ASSIGN_RE.sub(r"\1=[redacted]", cleaned)
    if len(cleaned) > limit:
        # 保留抛出点附近的栈尾，截掉的是更早的调用帧。
        cleaned = "...[truncated]\n" + cleaned[-(limit - 16) :]
    return cleaned


def redacting_exception_processor(
    _logger: Any,
    _method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Replace structlog's boolean ``exc_info`` with type, message, and stack.

    ``structlog.dev.set_exc_info`` only stores ``True``. ``JSONRenderer`` then
    emits ``"exc_info": true`` and drops the traceback. Call this processor
    after ``set_exc_info`` and before ``JSONRenderer``.
    """
    if "exc_info" not in event_dict:
        return event_dict
    exc_info = event_dict.pop("exc_info")
    if not exc_info:
        return event_dict
    if exc_info is True:
        exc_info = sys.exc_info()
    if isinstance(exc_info, BaseException):
        exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
    if not isinstance(exc_info, tuple) or len(exc_info) != 3 or exc_info[0] is None:
        return event_dict
    exc_type, exc, tb = exc_info
    event_dict["exc_type"] = getattr(exc_type, "__name__", None) or type(exc).__name__
    event_dict["exc_message"] = redact_secrets(str(exc), limit=500)
    event_dict["exc_stack"] = redact_secrets(
        "".join(traceback.format_exception(exc_type, exc, tb)),
        limit=6000,
    )
    for key, value in list(event_dict.items()):
        if key in {"exc_message", "exc_stack"} or not isinstance(value, str):
            continue
        event_dict[key] = redact_secrets(value, limit=2000)
    return event_dict


def processor_chain() -> list[Any]:
    """Processors used by worker and API JSON logs."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        redacting_exception_processor,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]


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
    if any(
        handler.get_name() == "openfarm.runtime.file"
        for handler in root_logger.handlers
    ):
        return

    handler = logging.StreamHandler(_RUNTIME_LOG_FILE)
    handler.set_name("openfarm.runtime.file")
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s trace_id=%(trace_id)s %(message)s"
        )
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
        processors=processor_chain(),
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=_runtime_log_stream()),
        cache_logger_on_first_use=True,
    )


logger = structlog.get_logger("openfarm")
