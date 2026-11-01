import json
from pathlib import Path
import sys
from xml.etree import ElementTree

import qualification_report as reporting


out, arm, source, target_text, raw, proof = sys.argv[1:]
out, raw, proof = Path(out), Path(raw), Path(proof)
labels = target_text.split()
(out / "targets.txt").write_text("\n".join(labels) + "\n")
events = out / "events.jsonl"
captured = reporting.capture(raw, events, Path("bazel-out"), out / "reports")
(out / "capture_exit").write_text("1" if captured["errors"] else "0")
identity = {k: k for k in ("commit_file", "source_before", "source_after", "final_head", "executed_arguments", "native_exit", "capture_exit")}
identity.update(expected_source=source, command_kind="test", execution_finished_at=(out / "finished").read_text().strip())
reporting.write_json(out / "context.json", identity)
verdict = reporting.verdict(events, reporting.manifest_path(events), out / "context.json", out / "targets.txt", ())
records = [json.loads(line) for line in events.read_text().splitlines()]
results = [r["testResult"] for r in records if "testResult" in r]
proof.mkdir(exist_ok=True, parents=True)
logs = "\n".join((out / r["path"]).read_text() for r in captured["reports"] if r["name"] == "test.log")
why = []
if verdict["errors"] or verdict["missing"] or set(labels) != set(verdict["expected_labels"]):
    why.append("incomplete original evidence or wrong target")
if len(results) != len(labels) or any(item.get("cachedLocally") or item.get("executionInfo", {}).get("cachedRemotely") or item.get("executionInfo", {}).get("strategy") != "remote" for item in results):
    why.append("expected one uncached remote original per target")
runner_exit = int((out/"runner_exit").read_text())
import re
from collections import Counter

case_records = {}
for report in captured['reports']:
    if report['name'] == 'test.xml':
        for case in ElementTree.parse(out / report['path']).iter('testcase'):
            name = case.get('name')
            if name in case_records:
                why.append('duplicate original case ' + name)
            case_records[name] = ('SKIP' if case.find('skipped') is not None else 'FAIL' if case.find('failure') is not None or case.find('error') is not None else 'PASS')
observations = {}
if arm == 'unit':
    required = ['ordinary/sack=true','ordinary/sack=false','cookie/sack=false','cookie_no_peer_sack/sack=false','cookie_no_timestamp/sack=false']
    for name in required:
        if case_records.get('TestSackPermittedAccept/'+name) != 'PASS':
            why.append('missing retained Go control: '+name)
else:
    expected = []
    for case in ('sack_and_timestamps','no_timestamps','no_peer_sack'):
        intentional_failure = arm == 'mutant' and case == 'sack_and_timestamps'
        desired = 'FAIL' if intentional_failure else 'PASS'
        if case_records.get('TestSynCookieSACK/'+case) != desired:
            why.append('wrong original status '+case+': '+str(case_records))
        expected.extend([(case,'1','1',''),(case,'2','1','2-3' if case == 'sack_and_timestamps' and not intentional_failure else '')])
        if not intentional_failure:
            expected.append((case,'3','3',''))
    rows = re.findall(r'COOKIE_SACK case=(\w+) step=(\d+) ack=(\d+) blocks=\[([0-9 ,\-]*)\]',logs)
    if Counter(rows)!=Counter(expected):
        why.append('wire ACK/SACK observations differ from expected')
    observations = dict(packets=rows,named_cases=case_records)
if arm=='mutant':
    failures={n for n,s in case_records.items() if s=='FAIL'}
    if verdict['status']!='FAIL' or int(verdict['native_exit'])!=3 or runner_exit==0 or 'SACK blocks at ACK offset 1 differ' not in logs or not failures or not failures.issubset({'TestSynCookieSACK/sack_and_timestamps','TestSynCookieSACK'}):
        why.append('the original failure did not isolate the post-accept SACK block: '+str(failures))
else:
    if verdict['status']!='PASS' or runner_exit or not case_records:
        why.append('target did not pass or has no original cases')
if 'SKIP' in case_records.values():
    why.append('unexpected original skip')
if arm=='unit':
    observations=dict(named_cases=case_records)

verdict.update(arm=arm, invocation=next(r["started"]["uuid"] for r in records if "started" in r),
               observed=observations, verified=not why, verification_errors=why)
reporting.write_json(proof / (arm + ".json"), verdict)
print(json.dumps({k: verdict[k] for k in ("arm", "invocation", "status", "case_counts", "verified", "verification_errors", "execution_to_verdict_seconds", "observed")}))
sys.exit(bool(why))
