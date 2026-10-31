"""Recover the six completion witnesses from the original Actions job."""
import datetime
import json
from pathlib import Path
import re
import subprocess

run = "41179457328"
repo = "tamird/gvisor"
root = Path("original")
modes = ("repeat_ack", "sack_repair")
public = "https://github.com/tamird/gvisor/actions/runs/" + run
job = json.loads(subprocess.check_output(["gh","api","repos/"+repo+"/actions/runs/"+run]))
assert job["head_sha"] == "8599f347ddc4399188fdc8013488bf94e2f542fd" and job["conclusion"] == "failure"
results = []
original = json.loads((root/"summary.json").read_bytes())
assert original["status"] == "FAIL" and not original["missing"]
invocation = None
finished = {}
with subprocess.Popen(["gh","run","view",run,"-R",repo,"--log"],stdout=subprocess.PIPE,text=True) as proc:
    for line in proc.stdout:
        match = re.search(r" INFO: Invocation ID: ([0-9a-f-]+)$",line.rstrip())
        if match:
            invocation = match[1]
        match = re.search(r"Z FENCE_DONE mode=(repeat_ack|sack_repair)\\n\^V--- PASS: TestLostTimeoutRepair/(repeat_ack|sack_repair) \([0-9.]+s\)$",line.rstrip())
        if match:
            assert invocation and match[1] == match[2]
            finished.setdefault(invocation,[]).append(match[1])
    assert proc.wait() == 0
sources = {"linux":"45cef2f3ba0df0814adbaf8e2cfaa40a4a9ee4df","stock":"45cef2f3ba0df0814adbaf8e2cfaa40a4a9ee4df","strict":"b9181a4c5ac30f8e33d0570d28223b4a8ca09aa2"}
assert len(finished) == len(sources)
for arm,source in sources.items():
    data = json.loads((root/(arm+".json")).read_bytes())
    assert data["arm"] == arm and data["source"] == source
    assert data["status"] == "INCOMPLETE" and data["errors"] == ["both live socket cases must complete"]
    assert data["native_exit"] == data["capture_exit"] == 0
    assert not any(data[k] for k in ("missing","failures","skips","zero_case_owners","all_skipped_owners"))
    assert data["case_counts"] == {"failed":0,"passed":3,"skipped":0}
    assert len(data["executed"]) == len(data["expected"]) == 1
    actual, = data["executed"]
    assert actual["owner"] == data["expected"][0] and actual["status"] == "PASSED" and not actual["cached_locally"]
    assert finished.pop(data["invocation"]) == list(modes)
    results.append(dict(arm=arm,source=source,invocation=data["invocation"],executed=data["executed"],modes=modes,
                        observations=data["observations"],execution_finished_at=data["execution_finished_at"]))
assert not finished
verdict = dict(status="PASS",original_run=public,original_artifact=13034064193,source_artifact_status=original["status"],
               reason="Completion markers were joined to the test PASS lines with literal backslash-n.",results=results,
               verified_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
Path("recovered").mkdir(exist_ok=True)
Path("recovered/verdict.json").write_text(json.dumps(verdict,indent=2)+"\n")
