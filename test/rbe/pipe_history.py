"""Collect observations on both exact revisions without treating a divergent prefix as a missing run."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

temp=Path(os.environ["RUNNER_TEMP"])
out=temp/"pipe-public"
out.mkdir(exist_ok=True)
carrier=os.environ["GITHUB_SHA"]
meta=json.loads((temp/"pipe-heads.json").read_text())
target="//pkg/tcpip/transport/tcp/test/e2e:tcp_rack_test"
case="TestRACKRepeatedRepairHistory"
rawpath=Path("bazel-testlogs/pkg/tcpip/transport/tcp/test/e2e/tcp_rack_test/test.log")
def git(*args):return subprocess.check_output(["git",*args])
def run(name,src):
    row={"source":src["source"],"instrumented":src["instrumented"],"case":case,"target":target,
         "start_ns":time.time_ns()}
    try:
        git("checkout","--quiet","--detach",src["instrumented"])
        assert git("rev-parse","HEAD^").decode().strip()==src["source"]
        assert len(git("diff","--name-only","HEAD^","HEAD").decode().splitlines())==3
        delta=git("diff","HEAD^","HEAD")
        (out/(name+"-instrumentation.patch")).write_bytes(delta)
        argv=["bazelisk","--bazelrc="+os.environ["FIFO_RC"],"test","--config=x86_64",
              "--strategy=TestRunner=local","--spawn_strategy=linux-sandbox","--jobs=4",
              "--test_env=GO_TEST_WRAP_TESTV=1","--test_env=GVISOR_TEST_CLOCK_SOURCE=calibrated",
              "--define=gvisor_test_clock_source=calibrated","--test_sharding_strategy=disabled",
              "--nocache_test_results","--test_output=summary","--test_timeout=240",
              "--test_filter=^"+case+"$","--build_event_json_file="+str(temp/(name+"-bep.jsonl")),target]
        row['argv']=argv
        with (out/(name+"-build.log")).open("w") as f:
            row["exit"]=subprocess.run(argv,stdout=f,stderr=subprocess.STDOUT).returncode
        if rawpath.exists():
            raw=rawpath.read_bytes()
            (out/(name+"-test.log.gz")).write_bytes(gzip.compress(raw,mtime=0))
            text=raw.decode(errors="replace")
            row["peer"]=[line.strip() for line in text.splitlines() if "HISTORY " in line]
            row["timers"]=[line.strip() for line in text.splitlines() if "DIAG TIMER " in line]
            row["named_pass"]=bool(re.search(r"--- PASS: "+case+r"\b",text))
            row["pipe_samples"]=sum("DIAG TOTAL " in line for line in text.splitlines())
            row["range_samples"]=sum("DIAG RANGE " in line for line in text.splitlines())
            row["outcomes"]=[x for x in row["peer"] if "HISTORY outcome " in x]
            row["raw_sha256"]=hashlib.sha256(raw).hexdigest()
            print(*row["peer"],sep="\n",flush=True)
            if not row["named_pass"]:print('\n'.join(text.splitlines()[-20:]),flush=True)
        else:print((out/(name+"-build.log")).read_text()[-4000:],flush=True)
        bep=[e['testResult'] for l in (temp/(name+"-bep.jsonl")).read_text().splitlines() if (e:=json.loads(l)).get('testResult')]
        row["bep"]=[(r["status"],r.get("cachedLocally",False),r.get("executionInfo",{}).get("cachedRemotely",False)) for r in bep]
        row["verified"]=row.get("named_pass") and row["exit"]==0 and row["bep"]==[("PASSED",False,False)] and bool(row.get("outcomes")) and row.get("range_samples",0)>0 and row.get("timers")
    except Exception as exc:
        row["exception"]=repr(exc)
    row["verified"]=bool(row.get("verified"))
    row["end_ns"]=time.time_ns()
    return row

rows={name:run(name,src) for name,src in meta.items()}
(out/"verdict.json").write_text(json.dumps({"carrier":carrier,"arms":rows,
    "origin":"https://github.com/tamird/gvisor/actions/runs/41098212501/artifacts/12997387247"},indent=2)+"\n")
raise SystemExit(0 if all(r["verified"] for r in rows.values()) else 1)
