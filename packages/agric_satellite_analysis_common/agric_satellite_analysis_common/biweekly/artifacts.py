"""Content-addressed local files for explicit offline runs, not an OSS adapter."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from .models import ReportError, digest


class LocalArtifactStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sha):
        if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ReportError("ARTIFACT_KEY_INVALID")
        path = self.root / (sha + ".pdf")
        if path.is_symlink():
            raise ReportError("ARTIFACT_KEY_INVALID")
        return path

    def put(self, content: bytes) -> str:
        if (
            not isinstance(content, bytes)
            or not content.startswith(b"%PDF-")
            or len(content) > 20_000_000
        ):
            raise ReportError("ARTIFACT_INVALID")
        sha = digest(content)
        path = self._path(sha)
        # 先完整写入同目录暂存文件，再硬链接原子发布；失败时不暴露半成品。
        fd, temporary = tempfile.mkstemp(prefix=".biweekly-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            if self.get(sha) != content:
                raise ReportError("ARTIFACT_CORRUPT")
        finally:
            Path(temporary).unlink(missing_ok=True)
        return sha

    def get(self, sha: str) -> bytes:
        path = self._path(sha)
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            raise ReportError("ARTIFACT_UNAVAILABLE") from None
        if digest(content) != sha or not content.startswith(b"%PDF-"):
            raise ReportError("ARTIFACT_CORRUPT")
        return content
