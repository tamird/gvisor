"""Reconcile the original BuildBuddy scheduler outputs using the fork qualifier."""
from datetime import datetime, timezone
from pathlib import Path
import json,sys,xml.etree.ElementTree as ET
import qualification_report as qr

out=Path(sys.argv[1]);source=sys.argv[2];events=out/"events.jsonl"
captured=qr.capture(out/"raw.jsonl",events,Path("bazel-out"),out/"reports")
(out/"capture_exit").write_text("1" if captured["errors"] else "0")
keys=("commit_file","source_before","source_after","final_head","executed_arguments","native_exit","capture_exit")
context={k:k for k in keys};context.update(expected_source=source,command_kind="test",execution_finished_at=(out/"finished").read_text().strip())
qr.write_json(out/"context.json",context)
v=qr.verdict(events,qr.manifest_path(events),out/"context.json",out/"targets",())
required={"TestDispatch_VFSRequirement/"+x for x in ("absent","false","true","duplicate","header_enables","header_disables","experiment_enables")}
required|={"TestScheduleTask_RequiresVFS","TestScheduleTask_VFSNotRequired","TestScheduleTask_NoExecutorSupportsVFS","TestEnqueueTaskReservation_PreferredExecutorRequiresVFS"}
cases=set()
for r in captured["reports"]:
 if r["name"]=="test.xml":
  for c in ET.parse(out/r["path"]).iter("testcase"):
   if c.find("skipped") is None and c.find("failure") is None and c.find("error") is None and c.get("status")!="notrun":
    cases.add(c.get("name",""))
v["required_vfs_cases"]={n:any(x==n or x.startswith(n+"/") for x in cases) for n in sorted(required)}
client="//enterprise/server/scheduling/scheduler_client:scheduler_client"
v["scheduler_client_built"]=False
for line in (out/"raw.jsonl").open():
 e=json.loads(line)
 if e.get("id",{}).get("targetCompleted",{}).get("label")==client and e.get("completed",{}).get("success"):v["scheduler_client_built"]=True
v["confirmed_pass"]=bool(v["status"]=="PASS" and not v["missing"] and v["scheduler_client_built"] and all(v["required_vfs_cases"].values()))
qr.write_json(out/"verdict.json",v)
sys.exit(not v["confirmed_pass"])
