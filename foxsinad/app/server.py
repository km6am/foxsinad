"""The desktop transport: serves the UI and carries engine messages over HTTP.

    GET  /          the UI (static/index.html, app.js, app.css)
    POST /cmd       one command as JSON -> the engine's reply as JSON
    GET  /events    the engine's events as server-sent events (text/event-stream)

Listens on 127.0.0.1 only. Requests must name this server in the Host
header (which stops DNS-rebinding pages from reaching it), and a POST must
be JSON from this origin, so other web pages can't send commands.
Standard library only. The Android app will carry the same messages over a
WebView bridge instead (docs/ui-protocol.md).
"""

import json
import queue
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static"
FILES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
         "/app.css": ("app.css", "text/css; charset=utf-8")}
MAX_BODY = 64 * 1024
KEEPALIVE_S = 5.0


def make_server(engine, port=8735):
    """A ThreadingHTTPServer bound to 127.0.0.1:port (0 picks a free port)."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "foxsinad"

        def log_message(self, fmt, *args):     # quiet: the engine logs what matters
            pass

        def _allowed_host(self):
            host = self.headers.get("Host", "")
            port_ = self.server.server_address[1]
            return host in (f"127.0.0.1:{port_}", f"localhost:{port_}")

        def _send(self, status, body=b"", ctype="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._allowed_host():
                return self._send(HTTPStatus.FORBIDDEN, b'{"error": "bad Host"}')
            path = self.path.split("?", 1)[0]
            if path == "/events":
                return self._events()
            if path in FILES:
                name, ctype = FILES[path]
                return self._send(HTTPStatus.OK, (STATIC / name).read_bytes(), ctype)
            self._send(HTTPStatus.NOT_FOUND, b'{"error": "not found"}')

        def do_POST(self):
            port_ = self.server.server_address[1]
            origin = self.headers.get("Origin")
            if not self._allowed_host() or (
                    origin and origin not in (f"http://127.0.0.1:{port_}",
                                              f"http://localhost:{port_}")):
                return self._send(HTTPStatus.FORBIDDEN, b'{"error": "bad Host or Origin"}')
            if self.path != "/cmd":
                return self._send(HTTPStatus.NOT_FOUND, b'{"error": "not found"}')
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                                  b'{"error": "send application/json"}')
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b'{"error": "too large"}')
            try:
                cmd = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(cmd, dict):
                    raise ValueError("a command is a JSON object")
            except ValueError as e:
                return self._send(HTTPStatus.BAD_REQUEST,
                                  json.dumps({"ok": False, "error": str(e)}).encode())
            reply = engine.handle(cmd)
            self._send(HTTPStatus.OK, json.dumps(reply).encode())

        def _events(self):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q = engine.subscribe()
            try:
                while True:
                    try:
                        msg = q.get(timeout=KEEPALIVE_S)
                        data = f"data: {json.dumps(msg)}\n\n"
                    except queue.Empty:
                        data = ": keepalive\n\n"     # also finds closed connections
                    self.wfile.write(data.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                engine.unsubscribe(q)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server
