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
        if self.server.previous_snapshot and self.path.split("?")[0] == "/github-status.json":
            data = self.server.previous_snapshot
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()

    def log_message(self, *args):
        pass

with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, subprocess.Popen(["chromedriver", "--port=9515"]) as driver:
    try:
        server.previous_snapshot = None
        threading.Thread(target=server.serve_forever, daemon=True).start()
        origin = f"http://127.0.0.1:{server.server_port}"
        wait(ready)
        session = response("/session", {"capabilities":{"alwaysMatch":{"browserName":"chrome","goog:chromeOptions":{"args":["--headless=new","--no-sandbox","--disable-dev-shm-usage","--window-size=1500,1000"]}}}})
        base = "/session/" + session["sessionId"]
        def execute(script, *args):
            return response(base+"/execute/sync", {"script":"return "+script, "args":args})
        def navigate(query, fresh=True):
            response(base+"/url", {"url":origin+"/?view=table"+query})
            marker = "GitHub snapshot ·" if fresh else "snapshot predates"
            wait(lambda: execute("document.querySelector('#all-reasons')?.textContent.match(/^All [0-9]+$/) && document.querySelector('#freshness-text').textContent.includes(arguments[0])", marker))
        def click(node):
            response(base+"/element/"+next(iter(node.values()))+"/click", {})
        response(base+"/goog/cdp/execute", {"cmd":"Browser.grantPermissions","params":{"origin":origin,"permissions":["clipboardReadWrite","clipboardSanitizedWrite"]}})
        navigate("")
        totals = execute("Object.fromEntries([...document.querySelectorAll('.attention-selector > summary')].map(s => [s.id.split('-')[1], Number(s.textContent.split(' · ').pop())]))")
        count = execute("Number(document.querySelector('#all-reasons').textContent.split(' ').pop())")
        assert len(totals) == 4 and sum(totals.values()) == count, (totals, count)
        for owner, total in totals.items():
            summary = execute("document.getElementById('attention-'+arguments[0]+'-summary')", owner)
            click(summary)
            click(execute("document.getElementById('attention-'+arguments[0]+'-summary').parentElement.querySelector('button')", owner))
            actual = execute("document.querySelectorAll('#work-table tr[data-node-id]').length")
            assert total == actual, (owner, total, actual)
            assert execute("new URL(location.href).searchParams.get('reason')") == "queue-"+owner
        navigate("&reason=review-changes#pr%3A16663")
        def copy_amendment():
            copy = wait(lambda: execute("[...document.querySelectorAll('#details button')].find(b => b.textContent === 'Copy range-diff') || null"))
            expected = execute("document.querySelector('#details .range-diff input').value")
            click(copy)
            wait(lambda: execute("document.querySelector('#details .range-diff [role=status]').textContent === 'Copied.'"))
            actual = execute("navigator.clipboard.readText()")
            assert actual == expected, (actual, expected)
            assert actual.startswith("git -C ~/code/gvisor range-diff "), actual
            for sha in ["ac7b4e0fd", "1dac5af96", "0b1ad2d6d", "e216e3075"]:
                assert sha in actual, (sha, actual)
            return actual
        copied = copy_amendment()
        issues = json.loads(Path("github-status.json").read_text())["issues"]
        closed = next(n["id"] for n in json.loads(Path("registry.json").read_text())["nodes"] if n["type"]=="issue" and issues.get(n["id"], {}).get("githubState")=="closed" and "contributorReview" not in n)
        navigate("&history=1#"+closed)
        assert execute("document.querySelector('#details').innerText.includes('Closed upstream.')")
        server.previous_snapshot = subprocess.check_output(["git", "show", "HEAD~2:github-status.json"])
        execute("localStorage.clear()")
        navigate("&reason=review-changes#pr%3A16663", fresh=False)
        partial = copy_amendment()
        assert partial == copied, (partial, copied)
        print(json.dumps({"partial_snapshot_clipboard":True,"total":count,"queues":totals,"clipboard":copied,"closed":closed}))
    finally:
        driver.terminate()
        server.shutdown()
