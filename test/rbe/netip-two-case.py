import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as XML

root=Path(sys.argv[1])
leaf="TestRestoreListenWithPreexistingConnection/CloseWaitProbeConnection/ReversedOrder"
results={}
for name in ("parent","candidate","route"):
 d=root/name
 result={"exit": (d/"exit").read_text().strip() if (d/"exit").exists() else None,
         "source": (d/"source").read_text().strip() if (d/"source").exists() else None,
         "diagnostics": (d/"diagnostics").read_text()[:2000] if (d/"diagnostics").exists() else None}
 xml=d/"test.xml"
 if xml.is_file() and xml.stat().st_size:
  cases=list(XML.parse(xml).iter("testcase"))
  result["cases"]=len(cases)
  result["skips"]=[c.get("name") for c in cases if c.find("skipped") is not None]
  if name=="route":
   target=[c for c in cases if c.get("name")=="TestRemoveRouteRemovesFirstMatchOnly" and c.find("skipped") is None and c.find("error") is None and c.find("failure") is None]
   result["valid"]=result["exit"]=="0" and len(target)==1
  else:
   log=(d/"test.log").read_text(errors="replace") if (d/"test.log").exists() else ""
   statuses=re.findall(r"(?m)^\s*--- (PASS|FAIL): "+re.escape(leaf)+r" \(",log)
   failures=re.findall("Unexpected probe endpoint state: want CLOSE-WAIT, got ESTABLISHED",log)
   result.update(trials=len(statuses),passed=statuses.count("PASS"),failed=statuses.count("FAIL"),matched_failure_lines=len(failures))
   result["valid"]=len(statuses)==96 and result["exit"] in ("0","3") and (result["failed"]==0 or len(failures)>=result["failed"])
 results[name]=result
(root/"verdict.json").write_text(json.dumps(results,indent=2)+"\n")
for n,x in results.items(): print(n,x)
if not all(x.get("valid") for x in results.values()): raise SystemExit(1)
