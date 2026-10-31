"""Emit one observation per fresh process; warm repetitions are never counted as independent."""
from pathlib import Path
from statistics import mean, median
import json
import sys

def main(source: Path, out: Path) -> None:
    data=json.loads(source.read_text())
    nginx = data["arms"]["nginx"]
    if nginx["exit"] != 0 or nginx["source"] != data["sources"]["GITHUB_SHA"] or nginx["owners"]["nginx"] != {"passed": 1,"failed": 0,"skipped": 0}:
        raise ValueError("unqualified Nginx arm")
    if len(data["peers"]) != 2 or not all(p["verified"].endswith("decoded=true") for p in data["peers"]) or "digest=" not in data["capture"]:
        raise ValueError("missing capture or cross-reader")
    if any(s.startswith("nginx") or s in ("missing benchmark observations","both cross-reader controls","stock binary identity","incomplete real benchmark","anonymous benchmark observations") for s in data["failures"]):
        raise ValueError("benchmark evidence incomplete")
    rows=data["rows"]
    indexed={(r["pair"],r["arm"],r["pass"]):r for r in rows}
    identities={(i,arm,n) for i in range(16) for arm in ("stock","candidate") for n in range(3)}
    if len(rows)!=len(identities) or set(indexed)!=identities: raise ValueError("incomplete pair")
    layouts={}
    for arm in ("stock","candidate"):
        samples=[]
        for phase,which in (("First",(0,)),("Repeated",(1,2))):
            all_rows=[]
            for i in range(16):
                group=[indexed[i,arm,n] for n in which]
                all_rows+=group
                numbers={name:mean(r[name] for r in group) for name in ("wall","cpu","allocated","allocs","stage","final","parts","scratch")}
                retained=sum(numbers[k] for k in ("stage","final","parts","scratch"))
                samples.append(f"BenchmarkNginxSave/{phase} 1 {numbers['wall']:.1f} ns/op {numbers['cpu']:.1f} cpu-ns/op {numbers['allocated']:.1f} B/op {numbers['allocs']:.1f} allocs/op {retained:.1f} buffer-B")
            layouts[arm+"/"+phase]={key:[min(r[key] for r in all_rows),median(r[key] for r in all_rows),max(r[key] for r in all_rows)] for key in ("bytes","stage","final","parts","scratch")}
        (out/(arm+".bench")).write_text("\n".join(samples)+"\n")
    (out/"layouts.json").write_text(json.dumps(layouts,indent=1)+"\n")

if __name__=="__main__": main(Path(sys.argv[1]),Path(sys.argv[2]))
