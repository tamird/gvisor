"""Exercise the local Pages checkout with the hosted Linux runner's Chrome."""
import json
from pathlib import Path
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

def wait(fn, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {fn}")

def response(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = Request("http://127.0.0.1:9515" + path, data=data, headers={"Content-Type":"application/json"})
    with urlopen(req, timeout=45) as reply:
        return json.load(reply)["value"]

def ready():
    try:
        return response("/status")["ready"]
    except OSError:
        return False

class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        filename = self.path.split("?", 1)[0].removeprefix("/")
        if self.server.legacy and filename in legacy:
            data = legacy[filename]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            super().do_GET()

    def log_message(self, *args):
        pass


legacy = {name: subprocess.check_output(["git", "show", "ab7a2286aecc6b29c5a72d1c5db40a3208570e15:" + name])
          for name in ("registry.json", "github-status.json")}
with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, subprocess.Popen(["chromedriver", "--port=9515"]) as driver:
    try:
        server.legacy = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        origin = f"http://127.0.0.1:{server.server_port}"
        wait(ready)
        session = response("/session", {"capabilities":{"alwaysMatch":{"browserName":"chrome","goog:chromeOptions":{"args":["--headless=new","--no-sandbox","--disable-dev-shm-usage","--window-size=1500,1000"]}}}})
        base = "/session/" + session["sessionId"]
        def execute(script, *args):
            return response(base+"/execute/sync", {"script":"return "+script, "args":args})
        card = "branch%3Abuildkite-source-syscalls"
        receipt = "https://github.com/tamird/gvisor/actions/runs/42656502954/artifacts/13693331942"
        def navigate(query):
            response(base + "/url", {"url": origin + "/?view=table&" + query + "#" + card})
            wait(lambda: execute("document.querySelector('#freshness-text').textContent.includes('GitHub snapshot ·')"))
            wait(lambda: execute("document.querySelector('#details').innerText.includes('Run the source presubmit')"))
            assert not execute("document.querySelector('#details .human-action') !== null")
            assert not execute("document.querySelector('input[data-reason=approve-run]') !== null")
        def shows_receipt():
            return execute("[...document.querySelectorAll('#details a')].some(a => a.href === arguments[0])", receipt)
        navigate("reason=approve-run")
        assert not shows_receipt()
        print("The changed browser app starts and opens the old deep link with both files from ab7a2286a.", flush=True)
        server.legacy = False
        def refreshed():
            if shows_receipt():
                return True
            execute("document.querySelector('#refresh').click()")
            return False
        wait(refreshed, 65)
        assert not execute("document.querySelector('#details .human-action') !== null")
        print("The same tab refreshes onto d42844b8a and links the 3,246-root result without asking for approval.", flush=True)
        navigate("reason=queue-agent")
        assert shows_receipt()
        assert execute("document.querySelector('#details .detail-top a').href") == "https://github.com/tamird/gvisor/commit/4eb27c408"
        row = execute("[...document.querySelectorAll('#details a')].find(a => a.href === arguments[0])", receipt)
        assert execute("arguments[0].textContent", row) == "Verified ARM64 default outputs ↗"
        response(base + "/execute/sync", {"script": """
            document.querySelector('#details').addEventListener('click', event => {
              window.receiptActivation = { trusted: event.isTrusted, url: event.target.closest('a').href };
            }, {once:true});
          """, "args":[]})
        before = response(base + "/window/handles")
        response(base + "/element/" + next(iter(row.values())) + "/click", {})
        assert execute("window.receiptActivation") == {"trusted": True, "url": receipt}
        wait(lambda: len(response(base + "/window/handles")) > len(before))
        print("A real click on the labeled result opened a separate tab at the artifact URL. The source commit link remained in the header.", flush=True)
    finally:
        driver.terminate()
        server.shutdown()
