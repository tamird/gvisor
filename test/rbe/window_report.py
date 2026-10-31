"""Reconcile original package results, individual cases, bytes, counters and real Save."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from statistics import median
import sys

from qualification_report import xml_cases

root = Path(sys.argv[1])
issues = []
arms = {}

def read(arm, file):
    p = root / arm / file
    return p.read_text(errors="replace").strip() if p.is_file() else ""

def need(ok, desc):
    if not ok:
        issues.append(desc)

sources = {"stock":os.environ["STOCK"],"fixed":os.environ["SOURCE"],"counters":os.environ["COUNTERS"],"nginx":os.environ["GITHUB_SHA"]}
images = {}
for arm in ("stock","fixed"):
    texts = re.findall(r"WINDOW-IMAGE hex=([0-9a-f]+)",read(arm,"state.test.log"))
    need(len(texts) == 1, f"{arm}: missing/duplicate wire image")
    if len(texts) == 1:
        images[arm] = bytes.fromhex(texts[0])
if len(images) == 2 and images["stock"] != images["fixed"]:
    sources["stock_read"] = sources["stock"]

for arm, expected in sources.items():
    argv = read(arm,"argv").splitlines()
    info = {"source":read(arm,"source"),"start":read(arm,"start"),"end":read(arm,"end"),"original_exit":read(arm,"exit"),
        "targets":[a for a in argv if a.startswith("//")],"xml":{},"go":{}}
    arms[arm] = info
    need(info["source"] == expected == read(arm,"source-after"),f"{arm}: unexpected source")
    need(info["original_exit"]=="0" and bool(info["start"] and info["end"]),f"{arm}: missing/time or unexpected Bazel exit")
    need(bool(read(arm,"commit")) and bool(read(arm,"product-sha256")),f"{arm}: missing source receipts")
    need(all((root/arm/k).is_file() and not read(arm,k) for k in ("source-before-status","source-after-status")),f"{arm}: dirty/unrecorded source")
    files = ("state","generated") if arm=="fixed" else ("nginx",) if arm=="nginx" else ("state",)
    expect_targets = ["//pkg/state:state_test","//pkg/state/tests:tests_test"] if arm=="fixed" else ["//test/rbe/window_nginx:checkpoint"] if arm=="nginx" else ["//pkg/state:state_test"]
    need(info["targets"] == expect_targets,f"{arm}: target mismatch")
    for file in files:
        try:
            x = xml_cases(root/arm/(file+".test.xml"))
        except (OSError,SyntaxError,ValueError) as err:
            x = {"error":str(err)}
        need(x.get("passed",0)>0 and x.get("failed")==0,f"{arm} {file}: XML")
        info["xml"][file] = x
        log = read(arm,file+".test.log")
        cases = re.findall(r"--- (PASS|FAIL|SKIP): (\S+)",log)
        if file=="nginx":
            m = re.findall(r"WINDOW-KERNEL root=\*kernel[.]Kernel attempts=(\d+) extensions=(\d+) bytes=(\d+)",log)
            # The shell may print the line twice if Go's benchmark header shares it.
            need(len(m)==1,f"nginx: {len(m)} kernel saves")
            if m:
                info["kernel"]={"attempts":int(m[0][0]),"extended":int(m[0][1]),"graph_payload_bytes":int(m[0][2])}
            need("BenchmarkCheckpointFile/EncodeStatefile" in log and "encoded-kernel-B" in log and not re.search(r"--- SKIP:",log),"nginx: absent real benchmark")
            continue
        passed = [case for result,case in cases if result=="PASS"]
        skipped = [case for result,case in cases if result=="SKIP"]
        info["go"][file]={"passed":len(passed),"skips":skipped}
        need(cases and not any(v=="FAIL" for v,_ in cases), f"{arm} {file}: missing/failing Go cases")
        required = ["TestGraphGrowingWindows"] if arm=="counters" else ["TestGrowingSliceWindows/graph"] if file=="generated" else ["TestWindowInterop","TestGraphGrowingWindows"] if arm=="fixed" else ["TestWindowInterop"]
        need(all(passed.count(k)==1 for k in required),f"{arm} {file}: missing named Go case")
    if arm in ("fixed","stock_read"):
        need("WINDOW-PEER decoded" in read(arm,"state.test.log"),f"{arm}: did not actually decode peer bytes")

pattern = re.compile(r"WINDOW-SAVE n=(\d+) order=(narrow|wide) sample=(\d+) save_ns=(\d+) bytes=(\d+) links=(\d+) buckets=(\d+)")
for arm in ("fixed","counters"):
    rows = [(int(n),order,int(i),int(ns),int(size),int(links),int(buckets)) for n,order,i,ns,size,links,buckets in pattern.findall(read(arm,"state.test.log"))]
    expected = {(n,o,i) for n in (128,256,512,1024,2048) for o in ("narrow","wide") for i in range(6)}
    keys = {(n,o,i) for n,o,i,*_ in rows}
    need(len(rows)==60 and keys==expected,f"{arm}: missing/extra samples {sorted(expected-keys)}")
    summary = {}
    for n in (128,256,512,1024,2048):
        summary[n]={}
        for o in ("narrow","wide"):
            warm=[(ns,size,links,buckets) for k,order,i,ns,size,links,buckets in rows if k==n and o==order and i>0]
            if warm:
                ns,sz,li,bu=zip(*warm)
                summary[n][o]={"median_ns":median(ns),"sizes":sorted(set(sz)),"links":sorted(set(li)),"buckets":[min(bu),max(bu)]}
    arms[arm]["warm_samples"] = summary

assert_name="TestGrowingSliceWindows/graph/new_tail"
m="mutant"
log=read(m,"generated.test.log")
cases=re.findall(r"--- (PASS|FAIL|SKIP): (\S+)",log)
failures=re.findall(r"graph_test[.]go:\d+: (.+)",log)
try:
    xml=xml_cases(root/m/"generated.test.xml")
except (OSError,SyntaxError,ValueError) as err:
    xml={"error":str(err)}
arms[m]={"source":read(m,"source"),"original_exit":read(m,"exit"),"start":read(m,"start"),"end":read(m,"end"),"xml":xml,"go":cases,"assertions":failures}
need(read(m,"source")==os.environ["MUTANT"]==read(m,"source-after") and read(m,"exit")=="3", "mutant: wrong source/exit")
need(xml.get("failed",0)>0 and xml.get("skipped")==0, "mutant: wrong XML")
need(sorted(cases)==sorted([("FAIL","TestGrowingSliceWindows"),("FAIL","TestGrowingSliceWindows/graph"),("FAIL",assert_name)]), "mutant: unexpected named cases")
need(failures == ["window 2: lost alias at offset 8"], f"mutant: wrong failures {failures}")
need(("PASS",assert_name) in re.findall(r"--- (PASS|FAIL|SKIP): (\S+)",read("fixed","generated.test.log")),"corrected missing new_tail")
wire = {arm:{"bytes":len(b),"sha256":hashlib.sha256(b).hexdigest()} for arm,b in images.items()}
result={"ok":not issues,"errors":issues,"wire_images":wire,"arms":arms,"verified_at":datetime.now(timezone.utc).isoformat()}
(root/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
print(json.dumps(result))
sys.exit(bool(issues))
