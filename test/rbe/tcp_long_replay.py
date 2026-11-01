import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

import qualification_report as reporting


def main():
    out, arm, source, label, raw, proof = sys.argv[1:]
    out, raw, proof = Path(out), Path(raw), Path(proof)
    captured = reporting.capture(raw, out / "events.jsonl", Path("bazel-out"), out / "reports")
    (out / "capture_exit").write_text("1" if captured["errors"] else "0")
    context = {k: k for k in ("commit_file", "source_before", "source_after", "final_head", "executed_arguments", "native_exit", "capture_exit")}
    context.update(expected_source=source, command_kind="test", execution_finished_at=(out / "finished").read_text().strip())
    reporting.write_json(out / "context.json", context)
    events = out / "events.jsonl"
    verdict = reporting.verdict(events, reporting.manifest_path(events), out / "context.json", out / "targets.txt", ())
    proof.mkdir(exist_ok=True, parents=True)
    logs="\n".join((out / r["path"]).read_text() for r in captured["reports"] if r["name"]=="test.log")
    rows=re.findall(r"--- (PASS|FAIL): TestListenerReusesReceiveBuffer/cookie=(true|false)/connection(\d+)",logs)
    bad = [line[line.index('listener did not accept'):][:160] for line in logs.splitlines() if 'listener did not accept data-bearing ACK' in line]
    diagnoses = [line.strip()[:230] for line in logs.splitlines() if any(x in line for x in ('too small for','SYN-ACK cookie =','did not enter SYN-cookie','waiting for SYN-ACK','missing SYN-ACK:', 'Poll returned','handshake payload', 'listener did not accept data-bearing ACK','--- FAIL: TestListenerReuses'))]
    expected='FAIL' if arm=='prototype' else 'PASS'
    normal=[(res,int(idx)) for res,cookie,idx in rows if cookie=='false'];cookies=[(res,int(idx)) for res,cookie,idx in rows if cookie=='true'];allpass=[('PASS',i) for i in range(8)]; expected_pressure=len(cookies)>=2 and cookies[:-1]==[('PASS',i) for i in range(len(cookies)-1)] and cookies[-1]==('FAIL',len(cookies)-1) and len(bad)==1
    ran=all(not r['cached_locally'] and not r['execution_info'].get('cachedRemotely') and r['execution_info'].get('strategy')=='remote' for r in verdict['executed'])
    complete=not verdict['missing'] and not verdict['errors'] and ran;ok=bool(complete and normal==allpass and (expected_pressure if arm=='prototype' else cookies==allpass) and verdict['status']==expected)
    keys=('invocation','source','status','native_exit','case_counts','expected_labels','execution_finished_at','verdict_available_at','execution_to_verdict_seconds')
    safe={k:verdict.get(k) for k in keys};safe.update(arm=arm,expected_status=expected,verified=ok,handshakes=rows,diagnostics=diagnoses[:18],errors_count=len(verdict['errors']),missing=verdict['missing'],executed_labels=sorted({r['owner']['label'] for r in verdict['executed']}),execution_remote_uncached=ran)
    reporting.write_json(proof/(arm+'.json'),safe); print(arm,'verified',ok,'missing',len(safe['missing']),'handshakes',rows,'diagnostics',safe['diagnostics']);return int(not ok)

if __name__=='__main__':sys.exit(main())
