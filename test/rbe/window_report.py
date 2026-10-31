"""Reconcile source, tested binaries, framed bytes, writer errors and actual Nginx archives."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from qualification_report import xml_cases

def main(root: Path) -> None:
    roots = {k:os.environ[k] for k in ("BASE","PRODUCT","STOCK","CANDIDATE","GITHUB_SHA")}
    checkouts = {"stock-build":("STOCK",[]),"stock-wire":("STOCK",["unit"]),"candidate-tests":("CANDIDATE",["unit","suite","analyzer"]),"candidate-race":("CANDIDATE",["unit","suite"]),"nginx":("GITHUB_SHA",["nginx"])}
    verdict = {"sources":roots,"arms":{},"wires":{},"rows":[],"peers":[],"failures":[]}
    errors = verdict["failures"]
    def guard(ok, why):
        if not ok: errors.append(why)
    for arm,(source, owners) in checkouts.items():
        p = root/arm
        def get(name):
            return (p/name).read_text().strip()
        try:
            arm_report = {"source":get("source"), "exit":int(get("exit")),"ended":get("end"),"owners":{}}
            verdict["arms"][arm] = arm_report
            guard(arm_report["source"]==get("source-after")==roots[source] and not get("source-before-status") and not get("source-after-status"),arm+" source mismatch")
            guard(arm_report["exit"]==0,arm+" Bazel failed")
            ids=re.findall(r"Invocation ID: ([0-9a-f-]{36})",get("bazel-diagnostics"))
            if ids: arm_report["invocation"]=ids[-1]
            if arm_report["exit"]:
                diag=get("bazel-diagnostics")
                arm_report["diagnosis"]=[s[:360] for s in diag.splitlines() if re.search(r"[.]go:\d+|^ERROR:|FAILED:|failed:|did not declare|undefined:|not found",s) and not re.search(r"(?i)token|header|secret|authorization|--",s)][:12]
            for owner in owners:
                stats=xml_cases(p/f"{owner}.test.xml")
                arm_report["owners"][owner]={k:stats[k] for k in ("passed","failed","skipped")}
                guard(stats["passed"]>0 and not stats["failed"],arm+"/"+owner+" missing or failed")
            if arm in ("stock-wire","candidate-tests"):
                ver=re.findall(r"STREAM-WIRE sha=([0-9a-f]{64}) bytes=(\d+)",get("unit.test.log"))
                guard(len(set(ver))==1,arm+" missing byte fixture")
                verdict["wires"][arm]=ver[0]
            if arm=="nginx":
                log=get("nginx.test.log")
                pair,role=None,None
                for line in log.splitlines():
                    if line.startswith("STREAM-CAPTURE "): verdict["capture"]=line
                    if line.startswith("STREAM-TRIAL "): pair,role=line.split()[1:]; pair=int(pair.split("=")[1]); role=role.split("=")[1]
                    if line.startswith("STREAM-ARCHIVE "):
                        fields=dict(x.split("=") for x in line.split()[1:])
                        guard(fields.pop("restored")=="true", "archive decode")
                        digest=fields.pop("digest")
                        guard(digest in verdict["capture"], "capture digest")
                        verdict["rows"].append({"pair":pair,"arm":role}|{k:int(v) for k,v in fields.items()})
                    if line.startswith("STREAM-CROSS "): verdict["peers"].append({"source":line,"verified":False})
                    if line.startswith("STREAM-PEER "): verdict["peers"][-1]["verified"]=line
                guard(re.search(r"^STREAM-DONE pairs=16 captures=1 cross=2 worker=\d+$",log,re.M), "incomplete real benchmark")
        except (OSError,ValueError,IndexError,AttributeError,KeyError) as exc:
            errors.append(arm+": "+str(exc)[:240])
    try:
        verdict["stock_binary"]=json.loads((root.parent/"phase-results/build-stock.json").read_text())
        guard(verdict["stock_binary"].get("source")==roots["STOCK"] and "digest" in verdict["stock_binary"],"stock binary identity")
    except (ValueError,OSError) as exc: errors.append(str(exc))
    guard(len(verdict["wires"])==2 and len(set(map(tuple,verdict["wires"].values())))==1,"different public graph bytes")
    rows=verdict["rows"]
    guard(len(rows)==96 and len({(r["pair"],r["arm"],r["pass"]) for r in rows})==96,"missing benchmark observations")
    guard(len(verdict["peers"])==2 and all(p["verified"] for p in verdict["peers"]), "both cross-reader controls")
    verdict.update(verified=datetime.now(timezone.utc).isoformat(),passed=not errors)
    (root/"verdict.json").write_text(json.dumps(verdict,indent=1)+"\n")
    print("qualified", verdict["passed"], "observations",len(rows),"failures",errors)
    if errors: raise SystemExit(1)

if __name__=="__main__": main(Path(sys.argv[1]))
