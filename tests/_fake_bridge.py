"""A stand-in for the Prodius desktop app's Voice Bridge, for tests.

Runs a stdlib HTTP server on a free loopback port in a background thread,
records every request, and answers from a per-path table of canned
responses. ``hold("/mcp/ask")`` makes that route block until ``release()``,
which lets tests cancel a request while it is still in flight. Set
``require_token`` to answer 401 to any other Bearer token.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeBridge:
    def __init__(self):
        self.requests: list[dict] = []
        self.responses: dict[str, tuple[int, object]] = {}
        self._held: dict[str, threading.Event] = {}
        self.require_token: str | None = None
        self._lock = threading.Lock()
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                path = self.path.split("?", 1)[0]
                with bridge._lock:
                    bridge.requests.append({
                        "method": self.command,
                        "path": path,
                        "query": self.path.partition("?")[2],
                        "headers": dict(self.headers),
                        "body": json.loads(raw) if raw else None,
                    })
                    held = bridge._held.get(path)
                if held is not None:
                    held.wait(10)
                status, payload = bridge.responses.get(path, (200, {"status": "ok"}))
                if bridge.require_token and \
                        self.headers.get("Authorization") != f"Bearer {bridge.require_token}":
                    status, payload = 401, {"error": "Invalid or missing auth token"}
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _handle

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def respond(self, path: str, payload, status: int = 200) -> None:
        self.responses[path] = (status, payload)

    def hold(self, path: str) -> None:
        self._held[path] = threading.Event()

    def release(self, path: str) -> None:
        self._held.pop(path).set()

    def calls(self, path: str) -> list[dict]:
        with self._lock:
            return [r for r in self.requests if r["path"] == path]

    def wait_for(self, path: str, count: int = 1, timeout: float = 10.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            calls = self.calls(path)
            if len(calls) >= count:
                return calls
            time.sleep(0.02)
        raise AssertionError(f"no request to {path} within {timeout}s")

    def close(self) -> None:
        for event in self._held.values():
            event.set()
        self._server.shutdown()
        self._server.server_close()
