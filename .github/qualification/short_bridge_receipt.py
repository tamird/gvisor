"""Keep the private bridge fixture's results separate from setup/test failures."""

import datetime
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

BASE = "471d6dff5229b99208bf6fec985601067f1f9d69"
JULY = "9f3e9a3fba9416ea1f5832d82923d95830479096"
SUFFIX = {"native": "native", "systrap": "runsc_systrap_overlay_directfs"}
PRODUCT = {"native": BASE, "current": BASE, "july": JULY}
CASES = {(l, a) for l in ("loopback", "bridge") for a in ("client", "server")}
TEMP = Path(os.environ["RUNNER_TEMP"])
OUT = TEMP / "bridge-receipts"


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def classify(text):
    errors = ("Operation not permitted", "Permission denied", "No space left on device",
              "no such target", "No such file or directory", "no matching toolchains",
              "failed to fetch", "download failed", "Checksum was", "not a mountpoint",
              "Network is unreachable", "RTNETLINK answers", "failed to list test cases",
              "TimeoutExpired", "ModuleNotFoundError", "FileNotFoundError", "RuntimeError",
              "AttributeError", "PermissionError", "not supported", "timed out", "Timeout")
    return [s for s in errors if s in text]


def safe_line(line):
    # The Bazel client and tests have a fresh, credential-free environment.
    if re.search(r"secret|password|api[_-]?key|authorization|bearer|cookie|token|gh[pousr]_|github_pat_|BEGIN.*PRIVATE", line, re.I):
        return None
    line = re.sub(r"(?:https?|grpcs?)://\S+", "<url>", line)
    line = re.sub(r"[A-Za-z0-9_=-]{48,}", "<digest>", line)
    return line.strip()[:360]


def failures(log):
    lines = log.splitlines()
    patterns = (r"SHORT_BRIDGE_EXPECTED_FAILURE", r"^ERROR:", r"Permission denied|No such file or directory",
                r"fatal error|^fatal:|error while loading|cannot execute|failed to load|failed to start",
                r"Traceback|(?:Error|Exception):|RTNETLINK answers|mount:|unexpected client address")
    keep = []
    for pattern in patterns:
        matching = [line for line in lines if re.search(pattern, line, re.I)]
        for line in matching[:3] + matching[-1:]:
            clean = safe_line(line)
            if clean and clean not in keep:
                keep.append(clean)
    return keep[:16]


def attempt_files(key):
    events = TEMP / (key + ".events")
    rows = [json.loads(l) for l in events.read_text().splitlines()] if events.is_file() else []
    log = TEMP / (key + ".buildlog")
    return rows, log.read_text(errors="replace") if log.is_file() else ""


def build(source, key, label, rc):
    rows, log = attempt_files(key)
    actions = []
    for e in rows:
        a = e.get("action", {})
        if not a or a.get("success"):
            continue
        owner = e.get("id", {}).get("actionCompleted", {})
        fd = a.get("failureDetail", {})
        actions.append({"owner": owner.get("label"), "primary_output": owner.get("primaryOutput"),
                        "mnemonic": a.get("type"), "exit": a.get("exitCode"), "failure":
                        {k: v.get("code") for k, v in fd.items() if isinstance(v, dict) and "code" in v}})
    complete = [e["completed"].get("success", False) for e in rows
                if e.get("id", {}).get("targetCompleted", {}).get("label") == label and "completed" in e]
    details = failures(log)
    canary = key == "error-canary"
    ok = (int(rc) != 0 and any(a["owner"] == label and a["mnemonic"] == "Genrule"
          and (a["primary_output"] or "").endswith("/short_bridge_canary_output") and a["exit"] for a in actions)
          and "SHORT_BRIDGE_EXPECTED_FAILURE" in (line.strip() for line in log.splitlines())) if canary else (int(rc) == 0 and True in complete)
    (OUT / (key + ".json")).write_text(json.dumps({"source": source, "label": label, "exit": int(rc),
        "target_completed": complete, "actions": actions[:12], "diagnostics": details, "verified": bool(ok), "at": now()}, indent=2))
    print(key, label, int(rc), ok)
    return not ok


def collect(source, name, rc):
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
    rows, log = attempt_files(name + "-test")
    target = ("short_bridge_native_canary" if name == "native" else "bridge_test_" + SUFFIX["systrap"])
    pkg = "test/rtnetlink/linux" if name == "native" else "test/rtnetlink"
    label = "//" + pkg + ":" + target
    results = []
    for event in rows:
        spec = event.get("id", {}).get("testResult", {})
        if spec.get("label") == label:
            r = event["testResult"]
            x = r.get("executionInfo", {})
            results.append({"status": r["status"], "strategy": x.get("strategy"),
                            "cached": r.get("cachedLocally", False) or x.get("cachedRemotely", False),
                            "time": r.get("testAttemptStart"), "duration": r.get("testAttemptDuration")})
    log_path = Path("bazel-testlogs") / pkg / target / "test.log"
    test_log = log_path.read_text(errors="replace") if log_path.is_file() else ""
    cells = []
    for line in test_log.splitlines():
        if line.startswith("TCP16908 {"):
            cells.append(json.loads(line.split(" ", 1)[1]))
    diagnostics = failures(log + "\n" + test_log)
    source_ok = source == PRODUCT[name]
    attempts_ok = (len(results) == 1 and results[0]["status"] == "PASSED"
                   and not results[0]["cached"] and results[0]["strategy"] == "local")
    cells_ok = len(cells) == 4 and {(r["link"], r["active_closer"]) for r in cells} == CASES
    for r in cells:
        c = r["client"]
        n = c["attempts"]
        ok = c["outcomes"].get("ok", 0)
        s = r["server"]
        cells_ok &= (n == 512 and 0 < ok <= n and sum(c["outcomes"].values()) == n
                     and (s if r["active_closer"] == "client" else c["counts"]).get(r["active_closer"] + " EOF", 0) > 0
                     and c["elapsed"] >= n * .045
                     and c["counts"].get("received", 0) >= ok and s.get("echoed", 0) >= ok
                     and s.get("accepted", 0) >= ok and s.get("closed", 0) <= s.get("accepted", 0))
        if ok == n:
            cells_ok &= (s.get("closed") == n and
                         (s if r["active_closer"] == "client" else c["counts"]).get(r["active_closer"] + " EOF") == n)
    result = {"source": source, "target": label, "platform": name, "fixture": hashlib.sha256(
        Path("test/rtnetlink/linux/short_connections.py").read_bytes()).hexdigest(),
        "kernel": platform.release(), "native_exit": int(rc), "test_results": results,
        "cells": cells, "diagnostics": diagnostics, "collected": now(),
        "gates": {"source": source_ok, "named_uncached_attempt": attempts_ok, "completed_cells": bool(cells_ok)}}
    (OUT / (name + ".json")).write_text(json.dumps(result, indent=2) + "\n")
    print(name, result["gates"], flush=True)
    return not (int(rc) == 0 and all(result["gates"].values()))


def finish():
    rows = {}
    for name in PRODUCT:
        p = OUT / (name + ".json")
        if p.exists():
            rows[name] = json.loads(p.read_text())
    ok = (set(rows) == set(PRODUCT) and all(all(r["gates"].values()) and r["native_exit"] == 0
                                           for r in rows.values())
          and len({r["fixture"] for r in rows.values()}) == 1)
    build_gates = {}
    for key, source, label in (
        ("error-canary", BASE, "//test/rtnetlink/linux:short_bridge_error_canary"),
        ("current-build", BASE, "//test/rtnetlink:bridge_test_runsc_systrap_overlay_directfs"),
        ("july-build", JULY, "//test/rtnetlink:bridge_test_runsc_systrap_overlay_directfs"),
    ):
        p = OUT / (key + ".json")
        record = json.loads(p.read_text()) if p.exists() else {}
        build_gates[key] = record.get("source") == source and record.get("label") == label and record.get("verified") is True
    ok &= all(build_gates.values())
    seen = []
    other = []
    clean = True
    for name, r in rows.items():
        for c in r["cells"]:
            cl, sv = c["client"], c["server"]
            for side, issues in (("client", {k: n for k, n in cl["outcomes"].items() if k != "ok"}),
                                 ("server", {k: n for k, n in sv.items() if k not in
                                             {"accepted", "received", "echoed", "client EOF", "closed"}})):
                for k, n in issues.items():
                    (seen if "ECONNRESET" in k else other).append([name, c["link"], c["active_closer"], side, k, n])
            clean &= (cl["outcomes"] == {"ok": 512} and all(sv.get(k) == 512 for k in ("accepted", "received", "echoed", "closed"))
                      and (sv if c["active_closer"] == "client" else cl["counts"]).get(c["active_closer"] + " EOF") == 512)
    verdict = ("INCOMPLETE" if not ok else "RESET_OBSERVED" if seen else
               "NO_ECONNRESET_OTHER_ERRORS" if other or not clean else "CLEAN_EXCHANGES_NO_ECONNRESET")
    (OUT / "verdict.json").write_text(json.dumps({"verdict": verdict, "resets": seen, "other_errors": other, "build_gates": build_gates, "all_exchanges_clean": bool(ok and clean and not seen and not other), "verified": now()}, indent=2) + "\n")
    print(verdict, seen)
    return not ok


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    if sys.argv[1:] and sys.argv[1] == "build":
        raise SystemExit(build(*sys.argv[2:]))
    if sys.argv[1:] and sys.argv[1] == "test":
        raise SystemExit(collect(*sys.argv[2:]))
    raise SystemExit(finish())
