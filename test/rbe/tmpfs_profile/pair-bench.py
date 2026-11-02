"""Run sixteen order-balanced, fresh-process pairs on a single frozen file."""
from pathlib import Path
import hashlib
import json
import os
import re
from statistics import median
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

if sys.argv[2] == "small":
    # Each ordinary state load reuses the decoder pool in the same process. Keep
    # these controls separate from the fresh-process whole-kernel samples above.
    small = []
    modes = {"ptr", "map", "slice"}
    for i in range(16):
        for arm in (("stock", "fixed") if i % 2 == 0 else ("fixed", "stock")):
            args = ["taskset", "-c", str(cpu), str(root / (arm + "-small")), "-test.cpu=1", "-test.run=^$",
                    "-test.bench=^BenchmarkDecoding$/^(ptr|map|slice)$/^state$", "-test.benchtime=32x", "-test.benchmem"]
            p = subprocess.run(args, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
            assert p.returncode == 0 and re.search(r"^PASS$", p.stdout, re.M), p.stdout[-1400:]
            lines = [line for line in p.stdout.splitlines() if line.startswith("BenchmarkDecoding/")]
            assert {line.split()[0].split("/")[1] for line in lines} == modes and len(lines) == 3, lines
            for line in lines:
                fields = line.split()
                assert fields[1] == "32" and {"ns/op", "B/op", "allocs/op"} <= set(fields[3::2]), line
                small.append({"arm": arm, "pair": i, "shape": fields[0].split("/")[1], "ns": float(fields[2]), "line": line})
            (out / "small-pairs.json").write_text(json.dumps(small, indent=2) + "\n")
    assert len(small) == 96
    (out / "small-finished.json").write_text(json.dumps({"rows": len(small), "cpu": cpu, "end": time.time_ns()}) + "\n")
    results = {}
    for shape in sorted(modes):
        def values(arm):
            return [row["ns"] for row in small if row["arm"] == arm and row["shape"] == shape]
        old, new = values("stock"), values("fixed")
        assert len(old) == len(new) == 16
        ratio = median(new) / median(old)
        slower = sum(b > a for a, b in zip(old, new))
        results[shape] = {"median_ratio": ratio, "slower_pairs": slower, "material": ratio > 1.05 and slower >= 13}
    for arm in ("stock", "fixed"):
        (out / (arm + "-small.bench")).write_text("\n".join(row["line"] for row in small if row["arm"] == arm) + "\n")
    (out / "small-gate.json").write_text(json.dumps(results, indent=2) + "\n")
    # Preserve this risk alongside the independently requested native comparison.
    print("small controls:", results, flush=True)
elif sys.argv[2] == "chain":
    expected = {f"BenchmarkDeferredChain/n{n}/seeded_{flag}" for n in (16,64,256,1024) for flag in ("true","false")}
    rows = []
    for pair in range(16):
        for arm in (("stock", "fixed") if pair % 2 == 0 else ("fixed", "stock")):
            args = ["taskset","-c",str(cpu),str(root / (arm+"-chain")),"-test.cpu=1","-test.run=^$",
                    "-test.bench=^BenchmarkDeferredChain$","-test.benchtime=4x","-test.benchmem"]
            result = subprocess.run(args, env=env,text=True,capture_output=True,timeout=120)
            lines = [line for line in result.stdout.splitlines() if line.startswith("BenchmarkDeferredChain/")]
            rows.append({"pair":pair,"arm":arm,"exit":result.returncode,"lines":lines})
            (out / "chain-bench.json").write_text(json.dumps(rows, indent=2)+"\n")
            assert result.returncode==0 and re.search(r"^PASS$",result.stdout,re.M)
            assert len(lines)==8 and {line.split()[0] for line in lines}==expected, lines
            assert all(line.split()[1]=="4" and {"ns/op","B/op","allocs/op"}<=set(line.split()[3::2]) for line in lines), lines
    assert len(rows)==32
elif sys.argv[2] == "benchmark":
    digests=freeze()
    rows=[]
    modes=["DecodeGC","DecodeStatefile"]
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
                rows.append({"pair":i,"arm":arm,"mode":mode,"line":line,"metrics":metrics})
                (out/"paired-bench.json").write_text(json.dumps(rows,indent=2)+"\n")
    assert len(rows)==64 and freeze()==digests
    (out/"bench-finished.json").write_text(json.dumps({"cpu":cpu,"rows":64,"source_hashes":digests,"end":time.time_ns()})+"\n")
else:
    raise ValueError(sys.argv[2])
