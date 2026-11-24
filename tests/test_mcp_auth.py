"""The check of personal tokens of the MCP server over HTTP (PHDCODE_MCP_AUTH_URL, deploy/replica).
Run: python tests/test_mcp_auth.py (no database is used; DATABASE_URL points at phdcode_test before the import)."""
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ["DATABASE_URL"] = os.environ.get("DATABASE_URL", "postgresql://x@127.0.0.1:1/x").rsplit("/", 1)[0] + "/phdcode_test"
assert os.environ["DATABASE_URL"].endswith("/phdcode_test")
from llm_engine import mcp_server  # noqa: E402

calls = []


class Who(BaseHTTPRequestHandler):
    def do_GET(self):
        calls.append(self.headers.get("Authorization"))
        good = self.headers.get("Authorization") == "Bearer good"
        self.send_response(200 if good else 401)
        self.end_headers()
        self.wfile.write(b"fumi@example.org" if good else b"no")

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), Who)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{srv.server_port}/whoami"
ok = mcp_server._authorized

assert ok(b"Bearer good", url)
assert ok(b"Bearer good", url) and len(calls) == 1, "a good token is kept: no second call"
assert ok(b"Bearer good", url, now=lambda: 1e12) and len(calls) == 2, "after 10 min it is checked again"
assert not ok(b"Bearer bad", url)
assert not ok(b"Bearer bad", url) and len(calls) == 4, "a bad token is never kept"
assert not ok(b"", url) and not ok(b"Basic x", url) and len(calls) == 4, "no Bearer: no call"
assert not ok(b"Bearer " + b"x" * 400, url), "too long"
srv.shutdown()
srv.server_close()
assert not ok(b"Bearer other", url), "the URL does not answer: refused"
print("test_mcp_auth: all checks pass")
