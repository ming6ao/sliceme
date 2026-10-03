"""The loopback review server (Python standard library only).

The server binds ``127.0.0.1`` and ``::1`` only, mints a write token, and
requires that token for every write.  It serves a fixed file list and a strict
Content Security Policy.  It calls :class:`sliceme.service.Service` in process,
so ``deliver`` reuses the engine's guard and never shells out to the CLI.
"""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..service import Service
from ..store import Store
from ..util import SlicemeError, config_path
from . import api
from .security import (
    constant_time_token_match,
    is_loopback_host,
    is_loopback_origin,
    mint_token,
)

__all__ = ["run_server"]

WEB_DIR = Path(__file__).resolve().parent / "web"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/diff.js": ("diff.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def discover_planes(roots: list[Path]) -> list[Path]:
    """Every plane root under the configured roots (a plane or its children)."""
    found: list[Path] = []
    for root in roots:
        root = Path(root).resolve()
        if config_path(root).is_file():
            found.append(root)
            continue
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir() and config_path(child).is_file():
                    found.append(child.resolve())
    seen: set[str] = set()
    unique: list[Path] = []
    for plane in found:
        key = str(plane)
        if key in seen:
            continue
        seen.add(key)
        unique.append(plane)
    return unique


def _plane_keys(planes: list[Path]) -> dict[str, Path]:
    keys: dict[str, Path] = {}
    for plane in planes:
        base = plane.name or str(plane)
        key = base
        counter = 2
        while key in keys:
            key = f"{base}-{counter}"
            counter += 1
        keys[key] = plane
    return keys


class _Handler(BaseHTTPRequestHandler):
    server_version = "SlicemeReview/1"
    protocol_version = "HTTP/1.1"

    # -- plumbing -------------------------------------------------------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Keep the foreground terminal readable; the client polls often.
        return

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    # -- GET ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in STATIC_FILES:
            name, content_type = STATIC_FILES[parsed.path]
            path = WEB_DIR / name
            if not path.is_file():
                self._error(HTTPStatus.NOT_FOUND, "asset missing")
                return
            self._send_bytes(HTTPStatus.OK, path.read_bytes(), content_type)
            return
        if parsed.path == "/api/state":
            self._handle_state(parse_qs(parsed.query))
            return
        if parsed.path == "/api/diff":
            self._handle_diff(parse_qs(parsed.query))
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def _plane_key(self, query: dict[str, list[str]]) -> str:
        keys: dict[str, Path] = self.server.plane_keys  # type: ignore[attr-defined]
        requested = (query.get("plane") or [""])[0]
        return requested if requested in keys else next(iter(keys))

    def _handle_state(self, query: dict[str, list[str]]) -> None:
        key = self._plane_key(query)
        service = self.server.service_for(key)  # type: ignore[attr-defined]
        params = {"commit": (query.get("commit") or [None])[0]}
        try:
            snapshot = api.state(service, params)
        except SlicemeError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        snapshot["planes"] = self.server.plane_list()  # type: ignore[attr-defined]
        snapshot["plane"] = key
        self._send_json(HTTPStatus.OK, snapshot)

    def _handle_diff(self, query: dict[str, list[str]]) -> None:
        key = self._plane_key(query)
        service = self.server.service_for(key)  # type: ignore[attr-defined]
        params = {
            "commit": (query.get("commit") or [None])[0],
            "file": (query.get("file") or [None])[0],
        }
        try:
            result = api.diff(service, params)
        except SlicemeError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._send_json(HTTPStatus.OK, result)

    # -- POST -----------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/action":
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        if not self._write_allowed():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "body must be JSON")
            return
        action = str(body.get("action") or "")
        if action not in {"comment", "decision", "deliver"}:
            self._error(HTTPStatus.BAD_REQUEST, f"unknown write action: {action}")
            return
        params = body.get("params") or {}
        if not isinstance(params, dict):
            self._error(HTTPStatus.BAD_REQUEST, "params must be an object")
            return
        key = str(params.get("plane") or "")
        keys: dict[str, Path] = self.server.plane_keys  # type: ignore[attr-defined]
        if not key or key not in keys:
            key = next(iter(keys))
        service = self.server.service_for(key)  # type: ignore[attr-defined]
        try:
            result = api.dispatch(service, action, params)
        except SlicemeError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except Exception as exc:  # pragma: no cover - defensive
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return
        self._send_json(HTTPStatus.OK, {"ok": True, "result": result})

    def _write_allowed(self) -> bool:
        token = self.headers.get("X-Sliceme-Token")
        if not constant_time_token_match(self.server.token, token):  # type: ignore[attr-defined]
            self._error(HTTPStatus.FORBIDDEN, "missing or invalid write token")
            return False
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if content_type != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
            return False
        if not is_loopback_origin(self.headers.get("Origin")):
            self._error(HTTPStatus.FORBIDDEN, "origin is not loopback")
            return False
        host = self.headers.get("Host")
        if host and not is_loopback_host(host):
            self._error(HTTPStatus.FORBIDDEN, "host is not loopback")
            return False
        return True


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], planes: dict[str, Path], token: str):
        super().__init__(address, _Handler)
        self.plane_keys = planes
        self.token = token
        self._local = threading.local()

    def plane_list(self) -> list[dict[str, str]]:
        return [{"key": key, "root": str(root)} for key, root in self.plane_keys.items()]

    def service_for(self, key: str) -> Service:
        services = getattr(self._local, "services", None)
        if services is None:
            services = {}
            self._local.services = services
        if key not in services:
            services[key] = Service(self.plane_keys[key], migrate=False)
        return services[key]


def build_server(
    plane_roots: list[Path], *, host: str = "127.0.0.1", port: int = 0
) -> _Server:
    """Build (but do not start) the review server, for tests and embedding."""
    if not is_loopback_host(host):
        raise SlicemeError("the review server binds loopback only")
    planes = discover_planes(list(plane_roots))
    if not planes:
        raise SlicemeError("no Sliceme plane found to review")
    # Run schema and migrations once, then close; request Services skip them.
    for plane in planes:
        store = Store(plane)
        store.close()
    token = mint_token()
    keys = _plane_keys(planes)
    return _Server((host, int(port)), keys, token)


def run_server(
    plane_roots: list[Path], *, host: str = "127.0.0.1", port: int = 0
) -> dict[str, Any]:
    server = build_server(plane_roots, host=host, port=port)
    token = server.token
    actual_host, actual_port = server.server_address[:2]
    url = f"http://{actual_host}:{actual_port}/#token={token}"
    print("sliceme review server", flush=True)
    print(f"  planes: {', '.join(server.plane_keys)}", flush=True)
    print(f"  url:    {url}", flush=True)
    print("  press Ctrl-C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    finally:
        server.server_close()
    return {"host": actual_host, "port": actual_port, "token": token, "url": url}
