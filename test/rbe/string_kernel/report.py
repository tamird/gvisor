"""Record the original native result, both graph loads and their real counters."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys


def main(temp):
    out = temp / "string-results"
    events = [json.loads(line) for line in (temp / "kernel.bep").open()]
    result = [e["testResult"] for e in events
              if e.get("id", {}).get("testResult", {}).get("label") == "//test/rbe/string_kernel:nginx"]
    log_path = out / "test.log"
    log = log_path.read_text(errors="replace") if log_path.is_file() else ""
    capture = re.findall(r"string-kernel capture=([a-f0-9]{64}) state=(\d+) pages=(\d+) metadata=(\d+)", log)
    arms = {}
    for mode in ("stock", "bounded"):
        pattern = (rf"string-kernel begin={mode} state=([a-f0-9]{{64}})\r?\n(.*?)"
                   rf"string-kernel end={mode} state=([a-f0-9]{{64}}) tasks=(\d+)")
        matches = re.findall(pattern, log, re.S)
        if len(matches) != 1:
            arms[mode] = {"complete": False}
            continue
        before, body, after, tasks = matches[0]
        data = re.findall(rf"string-study mode={mode} wire=(\d+) small=(\d+) payload=(\d+) slabs=(\d+) capacity=(\d+) direct=(\d+)", body)
        arms[mode] = {"complete": True, "before": before, "after": after, "tasks": int(tasks),
                      "frames": [dict(zip(("wire", "small", "payload", "slabs", "capacity", "direct"), map(int, row))) for row in data]}
    failures = []
    for event in events:
        what = event.get("action", {}).get("failureDetail", {}).get("message")
        why = event.get("aborted", {}).get("description")
        for text in (what, why):
            if text and not any(s in text.lower() for s in ("api-key", "auth", "header", "token", "secret", "password", "credential")):
                failures.append(text.splitlines()[0][:350])
    if not result:
        for line in (temp / "kernel-build.log").read_text(errors="replace").splitlines():
            if (line.startswith("ERROR: ") or any(t in line.lower() for t in ("no such file", "not found", "fatal:", "undefined reference", "cannot ", "unrecognized", "unexpected"))) and not any(s in line.lower() for s in ("api-key","header","token","secret","password","credential","authorization")):
                failures.append(line[:350])
    record = {"source": os.environ["SOURCE"], "study_source": "bd5e5d082f27cd82d0d9b8d78c8c6b80d2818864",
              "collected": datetime.now(timezone.utc).isoformat(), "exit": int((out / "bazel-exit").read_text()),
              "invocations": [e["started"].get("uuid") for e in events if "started" in e],
              "status": [r.get("status") for r in result], "cached": [r.get("cachedLocally", False) or r.get("executionInfo",{}).get("cachedRemotely", False) for r in result],
              "native_last_test_ms": [r.get("testAttemptStartMillisEpoch",0) for r in result],
              "test_durations_ms": [r.get("testAttemptDurationMillis",0) for r in result],
              "capture": capture, "arms": arms, "errors": list(dict.fromkeys(failures))[:8],
              "log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest() if log_path.is_file() else None}
    (out / "result.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record))
    assert record["exit"] == 0 and record["status"] == ["PASSED"] and record["cached"] == [False]
    assert len(capture) == 1 and all(int(value) > 0 for value in capture[0][1:])
    for arm in arms.values():
        assert arm["complete"] and arm["before"] == arm["after"] == capture[0][0]
        assert arm["tasks"] >= 5 and len(arm["frames"]) == 2
    assert arms["stock"]["tasks"] == arms["bounded"]["tasks"]
    for a,b in zip(arms["stock"]["frames"], arms["bounded"]["frames"]):
        assert all(a[k] == b[k] for k in ("wire","small","payload","direct"))
    assert arms["stock"]["frames"][1]["payload"] > 0


if __name__ == "__main__":
    main(Path(sys.argv[1]))
