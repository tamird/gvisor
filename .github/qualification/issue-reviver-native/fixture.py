"""Run the production launcher against one loopback-only GitHub issue list."""

import http.server
import json
import os
from pathlib import Path
import shlex
import socket
import ssl
import subprocess
import threading
from urllib.parse import parse_qs, urlsplit

tmp = Path(os.environ["RUNNER_TEMP"])
workspace = Path(os.environ["GITHUB_WORKSPACE"])
requests = []


class IssueList(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        route = urlsplit(self.path)
        query = parse_qs(route.query)
        requests.append((self.command, route.path, query, "Authorization" in self.headers))
        if route.path != "/repos/fixture/reviver/issues" or query != {"state": ["open"], "per_page": ["1000"]}:
            self.send_error(404)
            return
        payload = b'[{"number":101,"state":"open"}]'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def reject(self) -> None:
        requests.append((self.command, self.path, {}, "Authorization" in self.headers))
        self.send_error(405)
    do_POST = do_PATCH = do_PUT = do_DELETE = do_HEAD = reject

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    assert socket.if_nameindex() == [(1, "lo")], socket.if_nameindex()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 443), IssueList)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(tmp / "fixture.crt", tmp / "fixture.key")
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # This is the run command from the production workflow. Insert only the
    # real -dry-run flag before its positional subcommand.
    command = shlex.split((tmp / "revive.command").read_text().strip())
    if command[-1] != "revive":
        raise ValueError("production command no longer ends with revive")
    command[0] = os.path.expandvars(command[0])
    command.insert(-1, "-dry-run")
    env = {
        "PATH": os.environ["PATH"], "HOME": os.environ["RUNNER_TEMP"],
        "SSL_CERT_FILE": str(tmp / "fixture.crt"),
        "GITHUB_REPOSITORY": "fixture/reviver",
        "NO_PROXY": "*",
    }
    proc = subprocess.run(command, cwd="/", env=env, user=int(os.environ["QUAL_UID"]),
                          group=int(os.environ["QUAL_GID"]), extra_groups=[], timeout=60,
                          capture_output=True, text=True, check=False)
    server.shutdown()
    if proc.returncode:
        raise RuntimeError(f"launcher exit {proc.returncode}: {proc.stderr[:1200]}")
    expected = [
        "Loaded 1 issues from github.com/fixture/reviver",
        '"gvisor.dev/issue/101" is active: OK',
        "[dry-run: skipping change to issue 202]",
        "[nested/todos.go:2](https://github.com/fixture/reviver/blob/HEAD/nested/todos.go#L2): closed fixture",
        "Processed 2 TODOs, 0 were skipped (0 errors)",
    ]
    for item in expected:
        if item not in proc.stdout:
            raise ValueError(f"missing {item!r} in {proc.stdout[:2000]!r}")
    if str(workspace) in proc.stdout or len(requests) != 1 or requests[0][3]:
        raise ValueError(f"unexpected path, request count or authorization: {requests!r}")
    result = {
        "launcher_exit": proc.returncode, "request": requests[0], "network": "loopback only",
        "uid": int(os.environ["QUAL_UID"]), "observations": expected,
    }
    (tmp / "issue-result.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
