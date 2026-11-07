"""Project the original native TestRunner records without a service credential."""

import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlparse

WORK = Path(os.environ["RUNNER_TEMP"]) / "counter"
TARGETS = {f"//test/rbe/proc_counter:accounting_{cpu}" for cpu in ("amd64", "arm64")}
KEYS = ("native", "before", "installed", "enforced", "observable", "before_equal", "after_equal", "both_incremented")
MARKER = re.compile(r"^ACCOUNTING kernel=([0-9]+(?:\.[0-9]+){1,3}[-+.a-zA-Z0-9_]{0,32})"
                    + "".join(" " + k + "=([01])" for k in KEYS) + "$", re.M)


def result_log(label, output):
    uri = urlparse(output.get("uri", ""))
    d = re.search(r"/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/(\d+)$", uri.path)
    digest, size = (d[1], int(d[2])) if d else (output.get("digest"), int(output.get("length", 0)))
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        return {}
    result = {"sha256": digest}
    paths = [Path(unquote(uri.path))] if uri.scheme == "file" else []
    paths.extend(Path("bazel-out").glob("*/testlogs/" + label[2:].replace(":", "/") + "/test.log"))
    for path in paths:
        if not path.is_file() or size != path.stat().st_size or size > 1_000_000:
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            continue
        records = MARKER.findall(data.decode(errors="replace"))
        if len(records) == 1:
            kernel, *bits = records[0]
            result.update(kernel=kernel, **dict(zip(KEYS, [v == "1" for v in bits])))
        result["log_verified"] = True
        break
    return result


def main():
    report = {"source": "0f4de90fe214ba57ecba069aef0736d6ef732ba4", "exit": int((WORK / "exit").read_text()), "rows": {}}
    errors = set()
    bep = WORK / "probe.bep"
    if bep.is_file():
        for line in bep.open():
            e = json.loads(line)
            if re.fullmatch(r"[0-9a-f-]{36}", e.get("started", {}).get("uuid", "")):
                report["invocation"] = e["started"]["uuid"]
            action = e.get("action", {})
            if action and not action.get("success") and action.get("type") in ("CppCompile", "CppLink", "TestRunner"):
                errors.add(action["type"])
            label = e["id"].get("testResult", {}).get("label")
            if label not in TARGETS or "testResult" not in e:
                continue
            t = e["testResult"]
            info = t.get("executionInfo", {})
            row = {"passed": t.get("status") == "PASSED", "remote": info.get("strategy") == "remote",
                   "fresh": not (info.get("cachedRemotely") or t.get("cachedLocally"))}
            for item in t.get("testActionOutput", []):
                if item.get("name") == "test.log":
                    row.update(result_log(label, item))
            report["rows"].setdefault(label, []).append(row)
    text = (WORK / "bazel.log").read_text(errors="replace").lower()
    report["failed_actions"] = sorted(errors)
    report["signals"] = [s for s in ("no matching toolchain", "conflicting actions", "exec format error", "permission denied",
                                      "duplicate", "no registered executors", "no such file", "compilation failed",
                                      "unknown attribute", "timeout", "error:") if s in text]
    required = ("passed", "remote", "fresh", "log_verified", "native", "before", "enforced", "installed", "observable")
    report["verified"] = (report["exit"] == 0 and set(report["rows"]) == TARGETS and
                          all(len(rs) == 1 and all(rs[0].get(k) for k in required) for rs in report["rows"].values()))
    raw = json.dumps(report, indent=2)
    assert len(raw) < 10000
    (WORK / "receipt.json").write_text(raw + "\n")


if __name__ == "__main__":
    main()
