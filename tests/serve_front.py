"""
Dev proxy: serves front/ as static files and proxies all other requests
to the FastAPI backend at http://127.0.0.1:8771.

Usage (from project root, with venv active):
    python tests/serve_front.py
Then open http://localhost:8080
"""

import http.server
import urllib.request
import urllib.error
import os
import sys

FRONT_DIR = os.path.join(os.path.dirname(__file__), "..", "front")
BACKEND = "http://127.0.0.1:8771"
PORT = 8080

STATIC_EXTS = {".html", ".js", ".css", ".ico", ".png", ".jpg", ".svg", ".woff", ".woff2"}


class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=os.path.abspath(FRONT_DIR), **kwargs)

    def do_GET(self):
        if self._is_static():
            super().do_GET()
        else:
            self._proxy()

    def do_POST(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def _is_static(self):
        path = self.path.split("?")[0]
        if path == "/" or os.path.splitext(path)[1] in STATIC_EXTS:
            return True
        return False

    def _proxy(self):
        target = BACKEND + self.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "content-length")}

        req = urllib.request.Request(target, data=body, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(req) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ("transfer-encoding",):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() not in ("transfer-encoding",):
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(e.read())
        except urllib.error.URLError as e:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Backend unreachable: {e.reason}".encode())

    def log_message(self, fmt, *args):
        print(f"[proxy] {self.address_string()} - {fmt % args}")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    print(f"[proxy] serving front/ at http://localhost:{port}")
    print(f"[proxy] proxying API calls to {BACKEND}")
    with http.server.ThreadingHTTPServer(("", port), ProxyHandler) as srv:
        srv.serve_forever()
