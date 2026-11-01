import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

import qualification_report as reporting


def main():
    out, arm, source, label, raw, proof = sys.argv[1:]
    out, raw, proof = Path(out), Path(raw), Path(proof)
    (out / "targets.txt").write_text("\n".join(label.split()) + "\n")
    captured = reporting.capture(raw, out / "events.jsonl", Path("bazel-out"), out / "reports")
    (out / "capture_exit").write_text("1" if captured["errors"] else "0")
    context = {k: k for k in ("commit_file", "source_before", "source_after", "final_head", "executed_arguments", "native_exit", "capture_exit")}
    context.update(expected_source=source, command_kind="test", execution_finished_at=(out / "finished").read_text().strip())
    reporting.write_json(out / "context.json", context)
    events = out / "events.jsonl"
    verdict = reporting.verdict(events, reporting.manifest_path(events), out / "context.json", out / "targets.txt", ())
    proof.mkdir(exist_ok=True, parents=True)
    logs="\n".join((out / r["path"]).read_text() for r in captured["reports"] if r["name"]=="test.log")
    rows=re.findall(r'RACK_TRACE mode=(\w+) sacked=(\w+) allowance_ns=(\d+) expiry_ns=(\d+) at_expiry="([A-D]*)" first="([A-D]*)" first_ns=(-?\d+)',logs)
    keys=('source','status','native_exit','case_counts','expected_labels','execution_finished_at','verdict_available_at')
    safe={k:verdict.get(k) for k in keys};safe['invocation']=next(json.loads(line)['started']['uuid'] for line in events.open() if 'started' in json.loads(line))
    failures=[line.strip()[:220] for line in logs.splitlines() if any(x in line for x in ('missing ACK','at original expiry','early repair','RACK RTT','unexpected seq','restore','Panic:','--- FAIL:')) and not 'PASS' in line][-15:]
    executed=verdict['executed']
    remote=bool(executed) and all(not r['cached_locally'] and not r['execution_info'].get('cachedRemotely') and r['execution_info'].get('strategy')=='remote' for r in executed)
    matched=len(rows)==2 and rows[0][0]=='uninterrupted' and rows[0][4:6]==('A','A') and rows[0][2:4]==rows[1][2:4]
    coverage=remote and matched and not verdict['missing'] and {r['owner']['label'] for r in executed}=={label}
    safe.update(verified=coverage and ((arm=='complete' and verdict['status']=='PASS' and rows[1][4]=='A') or (arm!='complete' and verdict['status'] in ('PASS','FAIL'))),arm=arm,observations=rows,diagnostics=failures,executed_labels=sorted({r['owner']['label'] for r in executed}),missing=verdict['missing'],remote_uncached=remote)
    reporting.write_json(proof/(arm+'.json'),safe);print(json.dumps(safe));return int(not safe['verified'])

if __name__=='__main__':sys.exit(main())
