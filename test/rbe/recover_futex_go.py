"""Recover a passing futex Go TestRunner's named cases from its original log."""

import hashlib
import json
import os
from pathlib import Path
import re
import sys

import semop_owner_recovery as api


LABEL = "//pkg/sentry/kernel/futex:futex_test"
UID = "91a72315-529a-4a05-96f8-e08642791ab3"
ROOT = Path(os.environ["RUNNER_TEMP"]) / "records"


def cases(data):
    expected = sorted(re.findall(r"^func (Test\w+)\(t \*testing.T\)", Path("pkg/sentry/kernel/futex/futex_test.go").read_text(), re.M))
    assert len(expected) == 13
    log = data.decode(errors="replace")
    passed = sorted(re.findall(r"^--- PASS: (Test\w+) \(", log, re.M))
    problems = bool(re.search(r"^\s*--- (?:SKIP|FAIL):", log, re.M))
    return {"expected": expected, "passed": passed, "no_failures_or_skips": not problems,
            "verified": passed == expected and not problems}


def original():
    result = {"invocation": UID, "label": LABEL, "verified": False}
    try:
        rows = api.pages("GetTarget", {"selector": {"invocation_id": UID, "label": LABEL}}, "target")
        target, = [row for row in rows if row.get("label") == LABEL]
        assert api.identifier(target) == UID and target.get("status") in (5, "PASSED")
        result["target_passed"] = True
        actions = api.pages("GetAction", {"selector": {"invocation_id": UID, "target_label": LABEL}}, "action")
        uris = set()
        for action in actions:
            assert api.identifier(action) == UID and api.value(action, "targetLabel", "target_label") == LABEL
            uris.update(f["uri"] for f in action.get("file", []) if f.get("name") == "test.log")
        uri, = uris
        match = re.fullmatch(r"bytestream://(?:remote|app)\.buildbuddy\.io/(?:[^?#\s]+/)?blobs/([0-9a-f]{64})/(\d+)", uri)
        assert match and int(match[2]) <= 5_000_000
        data = api.request("GetFile", {"uri": uri}, raw=True)
        assert len(data) == int(match[2]) and hashlib.sha256(data).hexdigest() == match[1]
        result.update(cases(data), log_sha256=match[1], log_bytes=len(data))
    except Exception as exc:
        result["collection_error"] = type(exc).__name__
    api.write(ROOT / "original.json", result)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as out:
        print("complete=" + str(result["verified"]).lower(), file=out)


def main():
    if sys.argv[1] == "original":
        original()
        return
    first = json.loads((ROOT / "original.json").read_text())
    result = {"source": os.environ["CURRENT"], "label": LABEL, "original": first}
    if first["verified"]:
        result["verified"] = True
    else:
        result["fresh"] = cases((ROOT / "test.log").read_bytes())
        result["fresh"]["invocation"] = (ROOT / "invocation").read_text().strip()
        result["verified"] = bool(result["fresh"]["verified"] and (ROOT / "exit").read_text().strip() == "0"
                                  and re.fullmatch(r"[a-f0-9-]{36}", result["fresh"]["invocation"]))
    result["verdict_at"] = api.now()
    api.write(ROOT / "verdict.json", result)
    if not result["verified"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
