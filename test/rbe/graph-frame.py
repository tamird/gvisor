"""Verify each original Bazel action and retain only relevant diagnostics."""
import collections
import datetime
import json
import os
from pathlib import Path
import re
import xml.etree.ElementTree as et

root = Path(os.environ["RUNNER_TEMP"])
labels = ("//pkg/state:state_test", "//pkg/state/tests:tests_test")
out = {}
for arm in ("original", "fixed", "normal", "race"):
    folder = root / "records" / arm
    def read(name: str) -> str:
        p = folder / name
        return p.read_text(errors="replace") if p.is_file() else ""
    focus = arm in {"original", "fixed"}
    suites = labels[:1] if focus else labels
    groups = {label: {label} for label in suites}
    results = {}
    summaries = {}
    exec_end = {}
    build_end = None
    bep = root / (arm + ".bep")
    if bep.is_file():
        with bep.open() as stream:
            for line in stream:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                key = ev["id"]
                completed = key.get("targetCompleted", {}).get("label")
                for family in groups.values():
                    if completed and completed in family:
                        for child in ev.get("children", []):
                            for tag in ("targetCompleted", "testResult", "testSummary"):
                                if name := child.get(tag, {}).get("label"):
                                    family.add(name)
                if "testResult" in key:
                    name = key["testResult"]["label"]
                    results.setdefault(name, []).append(ev["testResult"]["status"])
                    exec_end[name] = ev["testResult"].get("testAttemptStartMillisEpoch", 0)
                    exec_end[name] = int(exec_end[name]) + int(ev["testResult"].get("testAttemptDurationMillis", 0))
                if "testSummary" in key:
                    summaries[key["testSummary"]["label"]] = ev["testSummary"]["overallStatus"]
                if "finished" in ev:
                    build_end = ev["finished"].get("finishTime")
    ok = read("source").strip() == os.environ["ORIGINAL" if arm == "original" else "FIXED"]
    ok &= read("exit").strip() == ("3" if arm == "original" else "0")
    owners = {}
    for label in suites:
        family = groups[label]
        action = [x for name, statuses in results.items() if name in family for x in statuses]
        summary = [v for k, v in summaries.items() if k in family]
        want = "FAILED" if arm == "original" else "PASSED"
        ok &= action == [want] and summary == [want]
        stem = label.split(":")[-1]
        cases = {}
        try:
            doc = et.fromstring(read(stem + ".test.xml"))
            for case in doc.iter("testcase"):
                name = case.get("name", "")
                tags = [c.tag for c in case if c.tag in {"skipped", "error", "failure"}]
                cases[name] = tags[0] if tags else "passed"
        except et.ParseError:
            pass
        counts = dict(collections.Counter(cases.values()))
        logs = read(stem + ".test.log")
        traces = re.findall(r"FRAME bytes=\[[^\n]+", logs)
        needed = ["TestGraphMapPayloadLength", "TestFrameObservation"] if focus else [
            "TestGraphMapPayloadLength", "TestGraphDeferredObjectDecode/multi_pass_round_trip",
            "TestGraphDeferredObjectDecode/deferred_trailing_data_error",
            "TestPrintGraphUnexportedInlineMapAndTrailingData"] if label == labels[0] else []
        relevant = {k: cases.get(k) for k in needed}
        ok &= bool(cases) and all(v == ("failure" if arm == "original" and k == "TestGraphMapPayloadLength" else "passed") for k, v in relevant.items())
        if focus:
            state = "ok" if arm == "original" else "truncated"
            ok &= len(traces) == 3 and traces[0] == traces[2] == "FRAME bytes=[2 7 11] load=ok print=ok decoded=map[7:11]" and traces[1].startswith(f"FRAME bytes=[1 7 11] load={state} print={state} decoded=")
            if arm == "original":
                ok &= "load accepted a one-byte map payload and returned map[7:11]" in logs
        else:
            ok &= counts.get("passed", 0) > 0 and not ({"failure", "error"} & counts.keys())
        owners[label] = dict(actions=action, summaries=summary, testEndUnixMs=max((v for k,v in exec_end.items() if k in family), default=None), counts=counts, required=relevant, observed=traces)
        if not ok:
            diags = [line.strip()[:400] for line in (logs + "\n" + read("buildlog")).splitlines() if re.search(r"^ERROR:|^FAIL:|\.go:\d+:|^--- FAIL|panic:|^WARNING:.*tim", line) and not re.search(r"authorization|remote.header|bes.header|token|key=|password",line,re.I)]
            owners[label]["diagnostics"] = list(dict.fromkeys(diags))[:30]
    out[arm] = dict(verified=ok, owners=owners, source=read("source").strip(), code=read("exit").strip(), finished=build_end or read("finished").strip(), invocations=re.findall(r"Invocation ID: ([\w-]+)",read("buildlog")))
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
verified = all(arm["verified"] for arm in out.values())
(root / "records" / "verdict.json").write_text(json.dumps(dict(complete=verified, arms=out, checkedAt=now),indent=2)+"\n")
raise SystemExit(not verified)
