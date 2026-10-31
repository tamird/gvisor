"""Record the selected syscall, nonempty checkpoint sequence and restore result."""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile
out=Path(sys.argv[1])
root=Path("bazel-testlogs/test/syscalls/socket_inet_loopback_test_runsc_systrap_directfs")
case="SocketInetLoopback.WildcardBoundPortReleasedOnClose"
r={"source":(out/"source").read_text().strip(),"exit":int((out/"exit").read_text()),"case":case}
text=(root/"test.log").read_text(errors="replace") if (root/"test.log").exists() else ""
restore=[line for line in text.splitlines() if "Executing: [" in line and " restore " in line and " --image-path " in line]
r["nonemptySavedImages"]=[int(re.search(r" --image-path \S*/state\.(\d+)\.",line)[1]) for line in restore]
r["abortedRestores"]=[line[-1200:] for line in text.splitlines() if "after restore error:" in line]
r["hostDiagnostics"]=[line[-1000:] for line in text.splitlines() if any(x in line for x in ("run error:","prepareSave error:","[  FAILED  ]","Failure","Expected:","Actual:","FATAL:"))][:30]
r["checkpointLogLines"]=[line.split("Executing: [",1)[1][-580:] for line in restore]
r["rows"]=[]
if (root/"test.xml").exists():
 for x in ET.parse(root/"test.xml").iter("testcase"):
  r["rows"].append({"name":x.get("classname","")+"."+x.get("name",""),"status":x.get("status"),"skip":x.get("result") in ("skipped","suppressed") or x.find("skipped") is not None,"errors":[e.get("message",e.text or "")[:1100] for e in x if e.tag in ("error","failure")]})
r["gtestPass"]=bool(re.search(r"\[\s*OK\s*\] "+re.escape(case)+r"\b",text))
# Group just the guest socket, module-trigger and checkpoint observations by sandbox.
r["traceSegments"]=[]
pattern=re.compile(r' (?:E (?:bind|connect|init_module)|X (?:bind|connect))\(')
def trace(name,data):
 lines=[l[-650:] for l in data.decode(errors="replace").splitlines() if pattern.search(l)]
 if lines:r["traceSegments"].append({"log":Path(name).name,"syscalls":lines})
z=root/"test.outputs/outputs.zip"
if z.exists():
 with ZipFile(z) as archive:
  for n in sorted(archive.namelist()):
   if "/logs/" in n and not n.endswith("/"):trace(n,archive.read(n))
else:
 for f in sorted((root/"test.outputs").glob("**/logs/*")):
  if f.is_file():trace(f.name,f.read_bytes())
s=r["nonemptySavedImages"];ok=r["exit"]==0 and r["gtestPass"] and r["rows"]==[{"name":case,"status":"run","skip":False,"errors":[]}] and len(s)>=8 and s==list(range(len(s))) and not r["abortedRestores"]
r["completedRestores"]=len(s) if ok else None
r["verified"]=ok
(out/"verdict.json").write_text(json.dumps(r,indent=2)+"\n")
print("exit",r["exit"],"GTest pass",r["gtestPass"],"nonempty checkpoints",len(s),"aborted restores",len(r["abortedRestores"]),"trace segments",len(r["traceSegments"]))
sys.exit(not ok)
