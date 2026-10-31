from pathlib import Path
import json
import sys

root=Path(sys.argv[1])
all_records={}
for flavor in ("jammy-iperf2","ding-iperf2","approved-iperf3"):
    path=root/flavor/"observations.json"
    observations=json.loads(path.read_text()) if path.exists() else []
    issues=[r for r in observations if "error" in r]
    valid=len(observations)==5 and not issues and all(r["receiver_bytes"]>0 for r in observations if "error" not in r)
    if flavor!="approved-iperf3" and valid:
        valid=all(r["aggregate_KiBps"] is not None for r in observations if r["streams"]>1)
    all_records[flavor]={"verified":valid,"cases":observations}
for flavor in ("2","3"):
    f=root/("go-parser-"+flavor)/"exit"
    log=root/("go-parser-"+flavor)/"test.log"
    passed=f.is_file() and f.read_text().strip()=="0" and log.is_file() and "--- PASS: TestActualLinuxIperfOutput" in log.read_text()
    all_records["go-parser-"+flavor]={"verified":passed}
(root/"verdict.json").write_text(json.dumps(all_records,indent=2)+"\n")
for label,data in all_records.items(): print(label, data["verified"], [r.get("error") for r in data.get("cases",[]) if "error" in r])
if not all(d["verified"] for d in all_records.values()): raise SystemExit(1)
