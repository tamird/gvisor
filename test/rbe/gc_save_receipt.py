import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

target = "//runsc/container:container_test"
case = "TestSaveCgroupMemoryLimit"
base = Path(sys.argv[1])
arms = {}
checks = {}

def read(path):
    return path.read_text(errors="replace") if path.is_file() else ""

def counters(text):
    return {k: int(v) for k, v in (line.split() for line in text.splitlines())}

for name in ("proposed", "ordinary"):
    d = base / name
    log = read(d / "test.log")
    cases = {}
    if (d / "test.xml").is_file():
        for node in ElementTree.parse(d / "test.xml").iter("testcase"):
            cases[node.get("name")] = ("FAIL" if any(node.find(k) is not None for k in ("error", "failure")) else "SKIP" if node.find("skipped") is not None else "PASS")
    matches = re.findall(r' SAVE_200M_OBS (\{[^\n]+\})', log)
    budgets = re.findall(r'SAVE_GC_BUDGET total=(\d+) memfile=(\d+) limit=(\d+) gogc=(\d+) handle=(true|false)', log)
    receipt = json.loads(matches[0]) if len(matches) == 1 else {}
    budget = dict(zip(("configured_total", "memoryfile", "go_limit", "gogc", "suspension_handle"), [*(int(x) for x in budgets[0][:-1]),budgets[0][-1]=="true"])) if len(budgets)==1 else {}
    before = receipt.get("before",{})
    after = receipt.get("after",{})
    old = counters(before.get("events",""))
    new = counters(after.get("events",""))
    changes = {k: new[k]-v for k,v in old.items() if k in new}
    source = read(d / "source").strip()
    status = receipt.get("rpc_error")
    killed = changes.get("oom_kill",0)>0
    sigkill = receipt.get("pidfd_ready")==1 and (receipt.get("proc_exit")==9 or (receipt.get("wait_status")==9 and receipt.get("wait_error")=="<nil>"))
    if status == "<nil>" and not killed and sum(receipt["sizes"].values())>0 and receipt.get("wait_status")==0 and receipt.get("wait_error")=="<nil>":
        outcome="SAVED"
    elif status and status!="<nil>" and killed and sigkill:
        outcome="SENTRY_SIGKILL_DURING_MEMCG_OOM"
    elif status and status!="<nil>" and not killed:
        outcome="SAVE_FAILED_WITHOUT_MEMCG_OOM_KILL"
    else:
        outcome="INCONCLUSIVE"
    clean = source==os.environ[name.upper()] and cases=={case:"PASS"} and read(d/"exit").strip()=="0" and len(matches)==len(budgets)==1
    clean = clean and all(x.get("limit")=="209715200" and x.get("swap")=="0" and int(x.get("peak",0))>0 for x in (before,after))
    clean = clean and str(receipt.get("sandbox_pid")) in before.get("members","").split() and changes.keys()>={"oom","oom_kill","max"} and budget.get("configured_total")==200<<20 and receipt.get("populated_files")==50000
    clean = clean and outcome!="INCONCLUSIVE" and (name!="ordinary" or (budget.get("suspension_handle") is False and budget.get("gogc",2**64)>0 and budget.get("gogc",2**64)<2**63))
    arms[name] = dict(source=source,target=target,executed=cases,missing=sorted({case}-cases.keys()),bazel_exit=read(d/"exit").strip(),invocations=re.findall(r"Invocation ID: ([0-9a-f-]+)",read(d/"diagnostics")),outcome=outcome,observation=receipt,go_budget=budget,event_deltas=changes,kernel_victim=read(d/"kernel-victim").strip(),source_digests=read(d/"blobs").splitlines(),started=read(d/"start").strip(),finished=read(d/"end").strip(),complete=clean)
    checks[name]=clean
blobs=[arm["source_digests"] for arm in arms.values()]
checks["identical_fixture_and_guest"] = all(len(b)==3 for b in blobs) and blobs[0][0]==blobs[1][0] and blobs[0][2]==blobs[1][2]
result=dict(upstream=os.environ["BASE"],carrier=os.environ["CURRENT"],arms=arms,checks=checks,verdict="MEASURED" if all(checks.values()) else "INCOMPLETE",verified=datetime.now(timezone.utc).isoformat())
(base/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
print({name: (arm["complete"],arm["outcome"],arm["go_budget"],arm["event_deltas"]) for name,arm in arms.items()},result["verdict"])
sys.exit(not all(checks.values()))
