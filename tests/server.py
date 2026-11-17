"""Throwaway HTTP server for downloader tests. Behaviour is configured per test; every request is logged."""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class TestServer:
    def __init__(self, files, ranges=True):
        self.files = files  # path -> bytes
        self.ranges = ranges  # honour Range headers
        self.fail: dict[int, int] = {}  # request number (1-based) -> status code to answer with
        self.truncate = set()  # request numbers answered with half a body
        self.delay = 0.0  # seconds to wait before answering
        self.bps: int | None = None  # throttle each response body to this many bytes per second
        self.deny = 0  # answer 403 to this many requests first
        self.token: str | None = (
            None  # when set, only URLs carrying ?t=<token> are valid; others get 403 (expired links)
        )
        self.log = []  # [{"path", "range", "status", "conn"}]
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                pass

            def do_GET(self):
                outer._handle(self)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path):
        return f"{self.base}/{path}"

    def requests(self, path=None):
        return [r for r in self.log if path is None or r["path"] == path]

    def _handle(self, h):
        with self._lock:
            number = len(self.log) + 1
            entry = {
                "path": h.path.lstrip("/").split("?")[0],
                "range": h.headers.get("Range"),
                "status": None,
                "conn": h.client_address[1],
            }
            self.log.append(entry)
            stale = self.token is not None and h.path.partition("?t=")[2] != self.token
            forced = self.fail.get(number) or (403 if self.deny or stale else None)
            if self.deny and not self.fail.get(number):
                self.deny -= 1
        if self.delay:
            time.sleep(self.delay)
        data = self.files.get(entry["path"])
        status, body, headers = 200, data, {}
        if forced:
            status, body = forced, b""
        elif data is None:
            status, body = 404, b""
        elif entry["range"] and self.ranges:
            start, _, end = entry["range"].removeprefix("bytes=").partition("-")
            start = int(start)
            end = min(int(end) if end else len(data) - 1, len(data) - 1)
            status, body = 206, data[start : end + 1]
            headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
        if self.ranges:
            headers["Accept-Ranges"] = "bytes"
        entry["status"] = status
        h.send_response(status)
        h.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            h.send_header(k, v)
        h.end_headers()
        out = body[: len(body) // 2] if number in self.truncate else body
        if self.bps:
            step = max(1, int(self.bps / 20))  # 20 slices a second
            for i in range(0, len(out), step):
                h.wfile.write(out[i : i + step])
                h.wfile.flush()
                time.sleep(step / self.bps)
        else:
            h.wfile.write(out)
        if number in self.truncate:
            h.close_connection = True
