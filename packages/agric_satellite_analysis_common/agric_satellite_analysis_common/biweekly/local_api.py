"""Standalone localhost HTTP API. Not registered in the production FastAPI app.

Bind 127.0.0.1 only. Requires BIWEEKLY_LOCAL_API=1 and BIWEEKLY_DEV_TOKEN.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .local_auth import authenticate
from .models import ReportError
from .trigger import TriggerService

# Production services/api/app/main.py must not import or include this router.

HTTP_ERRORS = {
    "LOCAL_API_DISABLED": 503,
    "DEV_TOKEN_REQUIRED": 503,
    "ACCESS_DENIED": 403,
    "LAND_NOT_IN_LOCAL_CATALOG": 404,
    "SEASON_NOT_FOUND": 404,
    "PERIOD_NOT_FOUND": 404,
    "JOB_NOT_FOUND": 404,
    "UNKNOWN_REFERENCE": 422,
    "REFERENCE_FIXTURE_MISSING": 422,
    "BACKFILL_WINDOW_UNBOUNDED": 422,
    "ARTIFACT_UNAVAILABLE": 409,
    "ARTIFACT_CORRUPT": 409,
}


def _json(handler: BaseHTTPRequestHandler, code: int, payload: dict):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:3214")
    handler.send_header("Access-Control-Allow-Headers", "X-Biweekly-Dev-Token, X-Biweekly-Dev-Tenant, X-Biweekly-Dev-Subject, Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(service: TriggerService):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _headers(self):
            return {k: v for k, v in self.headers.items()}

        def _auth(self):
            return authenticate(self._headers())

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > 65536:
                raise ReportError("BODY_TOO_LARGE")
            raw = self.rfile.read(length)
            if not raw:
                return {}
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ReportError("BODY_INVALID")
            return data

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:3214")
            self.send_header(
                "Access-Control-Allow-Headers",
                "X-Biweekly-Dev-Token, X-Biweekly-Dev-Tenant, X-Biweekly-Dev-Subject, Content-Type",
            )
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str):
            try:
                if method != "GET" or self.path.rstrip("/") != "/health":
                    self._auth()
                payload, extra = route(service, method, self.path, self._read_json if method == "POST" else lambda: {})
                if extra:
                    filename, data, mime = extra
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header(
                        "Content-Disposition", f'attachment; filename="{filename}"'
                    )
                    self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:3214")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                _json(self, 200 if method == "GET" else 200, payload)
            except ReportError as exc:
                _json(
                    self,
                    HTTP_ERRORS.get(exc.code, 400),
                    {"error": exc.code, "production_ready": False},
                )
            except json.JSONDecodeError:
                _json(self, 400, {"error": "BODY_INVALID", "production_ready": False})

    return Handler


def route(service: TriggerService, method: str, url: str, body_loader) -> tuple[dict, tuple | None]:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
    if method == "GET" and path == "/health":
        return {
            "ok": True,
            "service": "biweekly-local-trigger",
            "production_ready": False,
            "auto_publish": False,
            "bind": "127.0.0.1",
        }, None

    prefix = "/v1/lands/"
    if not path.startswith(prefix):
        raise ReportError("NOT_FOUND")
    rest = path[len(prefix):].split("/")
    # {land}/biweekly/...
    if len(rest) < 2 or rest[1] != "biweekly":
        raise ReportError("NOT_FOUND")
    land_id = rest[0]
    tail = rest[2:]

    if method == "GET" and tail == ["seasons"]:
        return service.list_seasons(land_id), None
    if method == "GET" and tail == ["periods"]:
        season = query.get("season")
        if not season:
            raise ReportError("SEASON_NOT_FOUND")
        return service.list_periods(land_id, season), None
    if method == "POST" and len(tail) == 3 and tail[0] == "periods" and tail[2] == "backfill":
        body_loader()
        return service.request_backfill(land_id, _season_for(service, land_id, query), tail[1]), None
    if method == "POST" and len(tail) == 3 and tail[0] == "periods" and tail[2] == "generate":
        body = body_loader()
        season = _season_for(service, land_id, query, body)
        ref = body.get("reference_planting_instance_id")
        return service.request_generate(
            land_id, season, tail[1], reference_planting_instance_id=ref
        ), None
    if method == "GET" and len(tail) == 2 and tail[0] == "jobs":
        return service.get_job(tail[1]), None
    if method == "GET" and len(tail) == 3 and tail[0] == "jobs" and tail[2] == "download":
        filename, data, mime = service.download(tail[1])
        return {}, (filename, data, mime)
    raise ReportError("NOT_FOUND")


def _season_for(service, land_id, query, body=None):
    season = (body or {}).get("season") or query.get("season")
    if not season:
        seasons = service.list_seasons(land_id)["seasons"]
        if len(seasons) == 1:
            return seasons[0]["season_id"]
        raise ReportError("SEASON_NOT_FOUND")
    return season


def serve(host="127.0.0.1", port=8765, service: TriggerService | None = None):
    if host not in {"127.0.0.1", "localhost"}:
        raise ReportError("BIND_MUST_BE_LOCALHOST")
    handler = make_handler(service)
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd
