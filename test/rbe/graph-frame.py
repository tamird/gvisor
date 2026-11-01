"""Verify each original Bazel action and retain only relevant diagnostics."""
import collections
import datetime
import base64
import http.client
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as et

if sys.argv[1:] == ["instrument"]:
    path = Path("pkg/state/graph_decode_values_unsafe.go")
    s = path.read_text()
    marker = "\t\to.base = reflect.MakeMapWithSize(ti.typ, hint).UnsafePointer()"
    assert s.count(marker) == 1
    path.write_text(s.replace(marker, '\t\tif o.size == 4096 { println("HINT", o.size, hint) }\n' + marker))
    raise SystemExit(0)

root = Path(os.environ["RUNNER_TEMP"])
labels = ("//pkg/state:state_test", "//pkg/state/tests:tests_test")
if sys.argv[1:] == ["prior"]:
    prior = {}
    for invocation in ("deca69a5-df47-41e6-b858-6d4cd3ebb8f6", "054fe976-6745-4755-b237-4e880dd60277"):
        try:
            h = http.client.HTTPSConnection("app.buildbuddy.io", timeout=12)
            h.request("POST","/api/v1/GetLog",json.dumps({"selector":{"invocation_id":invocation}}), {"Content-Type":"application/json","x-buildbuddy-api-key":os.environ["BUILDBUDDY_API_KEY"]})
            r = h.getresponse()
            if r.status != 200:
                prior[invocation] = {"http":r.status}
                continue
            raw = json.load(r).get("log",{})
            body = raw.get("contents","")
            if "\n" not in body and body:
                body=base64.b64decode(body).decode(errors="replace")
            lines = [l.strip()[:280] for l in body.splitlines() if re.search("SIGKILL|out.of.memory|OOM|signal: killed|^Killed|RESOURCE_EXHAUSTED|timed out|test exceeded|Exit 255",l,re.I) and not re.search("token|password|authorization|header=",l,re.I)]
            prior[invocation] = {"diagnostics":list(dict.fromkeys(lines))[-15:],"containsTestOutput":"Test output" in body,"logBytes":len(body)}
        except Exception as error:
            prior[invocation] = {"error":type(error).__name__}
    (root/"records"/"prior.json").write_text(json.dumps(prior))
    raise SystemExit(0)

out = {}
for arm in ("stacked", "nogo", "race"):
    folder = root / "records" / arm
    def read(name: str) -> str:
        p = folder / name
        return p.read_text(errors="replace") if p.is_file() else ""
    focus = arm == "stacked"
    suites = labels[:1] if focus else ("//pkg/state:state_nogo","//pkg/state:state_test_nogo") if arm == "nogo" else labels
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
    ok = read("source").strip() == os.environ["FIXED" if focus else "ORIGINAL"]
    ok &= read("exit").strip() == "0"
    owners = {}
    for label in suites:
        family = groups[label]
        action = [x for name, statuses in results.items() if name in family for x in statuses]
        summary = [v for k, v in summaries.items() if k in family]
        want = "PASSED"
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
        controls = ["TestGraphDeferredObjectDecode/multi_pass_round_trip", "TestGraphDeferredObjectDecode/deferred_trailing_data_error", "TestPrintGraphUnexportedInlineMapAndTrailingData"]
        needed = ["TestGraphMapPayloadLength","TestNestedMapHintObservation","TestFrameObservation",*controls] if focus else controls if arm == "race" and label == labels[0] else []
        relevant = {k: cases.get(k) for k in needed}
        ok &= (bool(cases) or arm == "nogo") and all(v == "passed" for k, v in relevant.items())
        if focus:
            state = "truncated"
            ok &= len(traces) == 3 and traces[0] == traces[2] == "FRAME bytes=[2 7 11] load=ok print=ok decoded=map[7:11]" and traces[1].startswith(f"FRAME bytes=[1 7 11] load={state} print={state} decoded=")
            matched = re.findall(r"^HINT 4096 (\d+)$", logs, re.M)
            hint = int(matched[0]) if len(matched) == 1 else 0
            ok &= ("restored nested map: 4096 entries" in logs and hint == 4096)
            traces.extend("HINT 4096 " + v for v in matched)
        elif arm == "nogo":
            markers = [arch for arch in ("amd64","arm64") if re.search(rf"{arch}\.\.\.", logs)]
            traces = markers
            ok &= len(markers) == 2
        else:
            ok &= counts.get("passed", 0) > 0 and not ({"failure", "error"} & counts.keys())
        owners[label] = dict(actions=action, summaries=summary, testEndUnixMs=max((v for k,v in exec_end.items() if k in family), default=None), counts=counts, required=relevant, observed=traces)
        if not ok:
            diags = [line.strip()[:400] for line in (logs + "\n" + read("buildlog")).splitlines() if re.search(r"^ERROR:|^FAIL:|\.go:\d+:|^--- FAIL|panic:|^WARNING:.*tim", line) and not re.search(r"authorization|remote.header|bes.header|token|key=|password",line,re.I)]
            owners[label]["diagnostics"] = list(dict.fromkeys(diags))[-40:]
            owners[label]["errorContext"] = [l.strip()[:280] for l in (logs+"\n"+read("buildlog")).splitlines() if re.search(r"signal:|SIGKILL|out of memory|^Killed|race detected|Fatal error|timed out|Exceeded|OOM|RESOURCE_EXHAUSTED|^FAIL|Exited with code",l,re.I) and not re.search(r"key|token|header|password",l,re.I)][-25:]
            owners[label]["lastCase"] = re.findall(r"=== RUN\s+(\S+)",logs)[-1:]
    out[arm] = dict(verified=ok, owners=owners, source=read("source").strip(), code=read("exit").strip(), finished=build_end or read("finished").strip(), invocations=re.findall(r"Invocation ID: ([\w-]+)",read("buildlog")))
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
verified = all(arm["verified"] for arm in out.values())
(root / "records" / "verdict.json").write_text(json.dumps(dict(complete=verified, arms=out, checkedAt=now, prior=json.loads((root/"records"/"prior.json").read_text()) if (root/"records"/"prior.json").exists() else {}),indent=2)+"\n")
raise SystemExit(not verified)
