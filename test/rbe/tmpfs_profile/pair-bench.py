"""Run sixteen order-balanced, fresh-process pairs on a single frozen file."""
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import sys
import time

root=Path(sys.argv[1])
capture=root/"capture"
out=root/"records"
cpu=min(os.sched_getaffinity(0))
env=dict(os.environ,GOMAXPROCS="1",GO_TEST_WRAP="0")
env.pop("XML_OUTPUT_FILE",None)
def freeze():
    return {name:hashlib.sha256((capture/name).read_bytes()).hexdigest() for name in ("checkpoint.img","pages.img","pages_meta.img","spec.json")}
digests=freeze()
rows=[]
modes=["DecodeGC","DecodeStatefile","EncodeStatefile"]
for i in range(16):
    for mode in modes[::(1 if (i//2)%2==0 else -1)]:
        for arm in (("stock","fixed") if i%2==0 else ("fixed","stock")):
            args=["taskset","-c",str(cpu),str(root/arm),"-checkpoint-dir="+str(capture),"-test.cpu=1","-test.run=^$","-test.bench=^BenchmarkCheckpointFile$/^"+mode+"$","-test.benchtime=1x","-test.benchmem"]
            p=subprocess.run(args,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=120)
            if p.returncode or not re.search(r"^PASS$",p.stdout,re.M):
                raise RuntimeError(p.stdout[-1300:])
            line,=[x for x in p.stdout.splitlines() if re.match("^BenchmarkCheckpointFile/"+mode+r"(-\d+)?\s",x)]
            name,n,*v=line.split()
            metrics={k:float(n) for n,k in zip(v[::2],v[1::2])}
            assert n=="1" and all(metrics.get(k,0)>0 for k in ("ns/op","cpu-ns","B/op","allocs/op")),line
            if mode=="DecodeStatefile":assert metrics["state-file-B"]==(capture/"checkpoint.img").stat().st_size,line
            if mode=="EncodeStatefile":assert metrics.get("encoded-kernel-B",0)>0,line
            rows.append({"pair":i,"arm":arm,"mode":mode,"line":line,"metrics":metrics})
            (out/"paired-bench.json").write_text(json.dumps(rows,indent=2)+"\n")
assert len(rows)==96 and freeze()==digests
(out/"bench-finished.json").write_text(json.dumps({"cpu":cpu,"rows":96,"source_hashes":digests,"end":time.time_ns()})+"\n")
