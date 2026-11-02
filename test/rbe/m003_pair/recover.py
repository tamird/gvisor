"""Read the single original stock screen's individual CAS outputs."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import traceback

import semop_owner_recovery as api

LABEL = "//test/rbe/m003_pair:screen"
HOST = "965547e68eec"
SOURCE = "30eb03cb88378a5805f951739ba8fadbd4a637a7"
OUT = Path(os.environ["RUNNER_TEMP"]) / "records"
NAMES = {"screen.json", "screen-held.json", "screen-held.json.events.json",
         "screen-timely.json", "screen-timely.json.events.json"}


def recover(rec):
    original = json.loads(Path("test/rbe/m003_pair/original.json").read_text())
    assert original["source"].split()[0] == SOURCE
    assert original["screen.status.json"] == {"attempts": [{"run": 1, "attempt": 1, "status": "PASSED", "cached": False}], "exit": 0, "diagnostics": []}
    rec["original"] = original
    api.write(OUT / "recovery.json", rec)
    rows = api.pages("SearchInvocation", {"query": {"command": "test", "pattern": LABEL, "host": HOST,
                     "updated_after": "2026-11-02T01:29:40Z", "updated_before": "2026-11-02T01:36:30Z"}, "count": 20}, "invocation")
    selected = [api.identifier(r) for r in rows if r.get("host") == HOST and r.get("pattern") in ([LABEL], LABEL)]
    rec["matching_invocations"] = selected
    api.write(OUT / "recovery.json", rec)
    uid, = selected
    inv, = api.pages("GetInvocation", {"selector": {"invocation_id": uid}}, "invocation")
    assert api.identifier(inv) == uid and inv.get("host") == HOST
    assert api.value(inv, "commitSha", "commit_sha") in ("", None, SOURCE)
    target, = [t for t in api.pages("GetTarget", {"selector": {"invocation_id": uid, "label": LABEL}}, "target") if t.get("label") == LABEL]
    assert api.identifier(target) == uid and target.get("status") in ("PASSED", 5)
    actions = api.pages("GetAction", {"selector": {"invocation_id": uid, "target_label": LABEL}}, "action")
    files = {}
    for action in actions:
        assert api.identifier(action) == uid and api.value(action, "targetLabel", "target_label") == LABEL
        for f in action.get("file", []):
            raw = f.get("name", "")
            name = raw.removeprefix("test.outputs/").removeprefix("test.outputs__")
            if name not in NAMES | {"test.xml"}:
                continue
            assert name not in files
            files[name] = f
    rec["advertised"] = sorted(files)
    api.write(OUT / "recovery.json", rec)
    assert set(files) == NAMES | {"test.xml"}
    output = OUT / "screen"
    output.mkdir(exist_ok=True)
    for name, f in sorted(files.items()):
        uri = f.get("uri", "")
        m = re.fullmatch(r"bytestream://(?:remote|app)\.buildbuddy\.io/(?:[^?#\s]+/)?blobs/([0-9a-f]{64})/(\d+)", uri)
        assert m and int(m[2]) <= 3*1024*1024, name
        content = api.request("GetFile", {"uri": uri}, raw=True)
        assert hashlib.sha256(content).hexdigest() == m[1] and len(content) == int(m[2])
        assert api.KEY.encode() not in content
        destination = OUT / "screen.xml" if name == "test.xml" else output / name
        if name != "test.xml":
            json.loads(content)
        destination.write_bytes(content)
        rec["files"][name] = {"sha256": m[1], "bytes": len(content)}
        api.write(OUT / "recovery.json", rec)
    summary = json.loads((output / "screen.json").read_text())
    assert summary["usable"] and summary["baseline_series"] == original["source"].split()[1]
    assert [r["schedule"] for r in summary["rows"]] == ["timely", "held"]
    assert all(r["valid"] and r["exit"] == 0 for r in summary["rows"])
    rec["activation"] = {r["schedule"]: r["mechanism"] for r in summary["rows"]}
    rec["status"] = "RECOVERED"
    api.write(OUT / "recovery.json", rec)
    print("Original recovered", uid, rec["activation"], flush=True)


if __name__ == "__main__":
    result = {"status": "INCOMPLETE", "files": {}, "test_executions": 0}
    try:
        recover(result)
    except Exception as exc:
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        result["error"] = {"type": type(exc).__name__, "function": frame.name, "line": frame.lineno}
        api.write(OUT / "recovery.json", result)
        sys.exit(1)
