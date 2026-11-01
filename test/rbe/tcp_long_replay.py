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
    cases = []
    for item in captured["reports"]:
        if item["name"] != "test.xml":
            continue
        for case in ET.parse(out / item["path"]).iter("testcase"):
            name = case.get("name", "")
            if case.find("failure") is not None or case.find("error") is not None:
                state = "failed"
            elif case.find("skipped") is not None:
                state = "skipped"
            else:
                state = "passed"
            cases.append((name, state))
    wanted = ("TestRestoreRACKReorderWindow",)
    migrations = {w: [(n, s) for n,s in cases if w in n] for w in wanted}
    remote = all(not x['cached_locally'] and not x['execution_info'].get('cachedRemotely') and x['execution_info'].get('strategy')=='remote' for x in verdict['executed'])
    owners = sorted({x['owner']['label'] for x in verdict['executed']})
    variants = ('--config=race' in (out/'executed_arguments').read_text().splitlines()) == (arm=='race')
    valid = (verdict['status']=='PASS' and not verdict['missing'] and not verdict['errors'] and remote and variants
             and sorted(label.split())==owners and all(value and all(s=='passed' for _,s in value) for value in migrations.values()))
    keys=('source','status','native_exit','case_counts','execution_finished_at','verdict_available_at','execution_to_verdict_seconds')
    safe={k:verdict.get(k) for k in keys}
    safe['invocation']=next(json.loads(line)['started']['uuid'] for line in events.read_text().splitlines() if 'started' in json.loads(line))
    safe.update(arm=arm, verified=valid, executed_labels=owners, errors_count=len(verdict['errors']), missing=verdict['missing'],
                remote_uncached=remote, correct_race_mode=variants, tested={k: len(v) for k,v in migrations.items()},
                failures=[n for n,s in cases if s=='failed'], skips=[n for n,s in cases if s=='skipped'])
    reporting.write_json(proof/(arm+'.json'),safe)
    print(json.dumps(safe))
    return int(not valid)

if __name__ == '__main__':
    sys.exit(main())
