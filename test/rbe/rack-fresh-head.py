"""Preserve each actual loss response, including an incomplete or stalled arm."""
import hashlib
import json
import os
from pathlib import Path
import re
import time
from xml.etree import ElementTree

root = Path(os.environ["RUNNER_TEMP"])
folder = root / "records"
fixture = hashlib.sha256((root/"packet.go").read_bytes()).hexdigest()
verdict = {"carrier": (folder/"carrier").read_text().strip(), "fixture": fixture, "arms": {}}
for arm in ("linux", "stock", "select"):
    d = folder/arm
    entry = {"passed": False}
    verdict["arms"][arm] = entry
    for name in ("started", "finished", "invocation", "head", "fixture", "exit"):
        if (d/name).is_file():
            entry[name] = (d/name).read_text().strip()
    target = "tcp_rack_native_test" if arm == "linux" else "tcp_rack_netstack_test"
    label = "//test/packetimpact/tests:"+target
    try:
        dest = d/target
        log = (dest/"test.log").read_text(errors="replace")
        cases = {c.get("name", ""):[e.tag for e in c if e.tag in ("error","failure","skipped")]
                 for c in ElementTree.parse(dest/"test.xml").iter("testcase")}
        named = [v for k,v in cases.items() if k.endswith("TestRACKFreshTimeoutRetransmission")]
    except (OSError, ElementTree.ParseError, ValueError):
        log, named = (dest/"test.log").read_text(errors="replace") if (dest/"test.log").exists() else "", []
    entry["named"] = named
    matches = re.findall(r"RACK_AFTER_TIMEOUT native=(true|false) repairs=\[([0-9 ]*)\] mss=(\d+)", log)
    entry["trace"] = [{"native": native, "repairs": [int(i) for i in seq.split()], "mss": int(mss)}
                      for native, seq, mss in matches]
    entry["diagnostics"] = [l.strip()[:450] for l in log.splitlines() if re.search(r"tcp_rack_test.go:\d+:|^--- FAIL|^panic:", l)
                            and not re.search(r"https?://|(?i:api[_-]?key|header|token|secret|bearer)", l)][-12:]
    if not entry["trace"] and (d/"log").is_file():
        entry["bazel"] = [l.strip()[:400] for l in (d/"log").read_text(errors="replace").splitlines()
                          if re.search(r"(^ERROR:|undefined:|does not exist|cannot use|not declared)", l)
                          and not re.search(r"https?://|grpc|(?i:api[_-]?key|header|token|secret|bearer)", l)][-10:]
    events = []
    if (root/(arm+".bep")).is_file():
        for line in (root/(arm+".bep")).read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    actual = {label}
    for e in events:
        if e["id"].get("targetCompleted", {}).get("label") == label:
            actual.update(child[k]["label"] for child in e.get("children",[]) for k in ("targetCompleted","testSummary","testResult") if "label" in child.get(k,{}))
    attempts = [{"status":v["status"],"cached":bool(v.get("cachedLocally") or v.get("executionInfo",{}).get("cachedRemotely")),
                 "strategy":v.get("executionInfo",{}).get("strategy")}
                for e in events if "testResult" in e and e["id"]["testResult"].get("label") in actual for v in [e["testResult"]]]
    summaries = [e["testSummary"]["overallStatus"] for e in events if "testSummary" in e and e["id"]["testSummary"].get("label") in actual]
    entry.update(attempts=attempts, summaries=summaries)
    trace = entry["trace"]
    expected_native = "true" if arm == "linux" else "false"
    entry["passed"] = bool(entry.get("exit") == "0" and entry.get("fixture") == fixture
                           and entry.get("head") == os.environ["MASTER" if arm == "stock" else "PRODUCT"]
                           and named == [[]] and summaries == ["PASSED"] and len(attempts) == 1
                           and attempts[0]["status"] == "PASSED" and not attempts[0]["cached"]
                           and attempts[0]["strategy"] == "remote" and len(trace) == 1
                           and trace[0]["native"] == expected_native and trace[0]["mss"] > 0)
    if arm == "select":
        entry["passed"] = entry["passed"] and trace[0]["repairs"] == [2, 3]
verdict.update(complete=all(r["passed"] for r in verdict["arms"].values()), verified_unix=time.time())
(folder/"verdict.json").write_text(json.dumps(verdict,indent=2)+"\n")
raise SystemExit(not verdict["complete"])
