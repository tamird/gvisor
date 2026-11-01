"""Compare the existing graph reader on one fixed checkpoint and the native guest."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from xml.etree import ElementTree

root=Path(os.environ["RUNNER_TEMP"])/"pair"
out=root/"records"
out.mkdir(parents=True, exist_ok=True)
phase=sys.argv[1]
sources={key:os.environ[key.upper()] for key in ("stock","fixed")}
gnu=["docker","exec","tcp-compiler"]
bz=gnu+["bazel","--bazelrc="+os.environ["RUNNER_TEMP"]+"/tcp.bazelrc"]
opts=["--config=x86_64","--spawn_strategy=remote,local","--noremote_upload_local_results","--strategy=GoStaticAnalysis=local","--strategy=GoStandardLibraryAnalysis=local","--remote_download_outputs=toplevel","--jobs=24"]
def record(name, data):
    (out/name).write_text(json.dumps(data,indent=2)+"\n")
def command(name, args, *, allow_failure=False, get_stdout=False):
    start=time.time_ns()
    stdout=root/(name+".stdout")
    log=root/(name+".log")
    with stdout.open("wb") as o, log.open("wb") as e:
        p=subprocess.run(args,stdout=o,stderr=e)
    text=log.read_text(errors="replace")
    end=time.time_ns()
    line=[x for x in text.splitlines() if re.search(r"^ERROR:|^FAILED:|Build did NOT complete successfully|Invocation ID:|loading.*failed",x)]
    record(name+"-command.json",{"exit":p.returncode,"start":start,"end":end,"diagnostics":[v[:360] for v in line[-10:] if not re.search(r"remote_header|api.key|authorization|cookie=",v,re.I)]})
    if p.returncode and not allow_failure:
        raise RuntimeError(f"{name} exited {p.returncode}: {line[-6:]}")
    return stdout.read_text() if get_stdout else p.returncode
def select(arm):
    sha=sources[arm]
    subprocess.run(["git","checkout","-q","--detach",sha],check=True)
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==sha
    subprocess.run(["git","diff","--exit-code","--quiet"],check=True)
def output_file(name, target):
    found=command(name,bz+["cquery",*opts,"--output=files",target],get_stdout=True).splitlines()
    matches=[Path(x).absolute() for x in found if x and subprocess.run(gnu+["test","-f",x,"-a","-x",x]).returncode==0]
    if len(matches)!=1:raise ValueError((target,matches))
    return matches[0]
def prepare():
    original_bench=[]
    for arm in ("stock","fixed"):
        select(arm)
        original_bench.append(subprocess.check_output(["git","rev-parse","HEAD:pkg/state/tests/bench_test.go"],text=True).strip())
        label="//pkg/state/tests:tests_test"
        command(arm+"-build",bz+["build",*opts,label])
        exe=output_file(arm+"-build-path",label)
        subprocess.run(gnu+["cp",str(exe),str(root/arm)],check=True)
    assert original_bench[0]==original_bench[1]==os.environ["FIXTURE_BLOB"],original_bench
    hashes={arm:subprocess.check_output(gnu+["sha256sum",str(root/arm)],text=True).split()[0] for arm in ("stock","fixed")}
    assert len(set(hashes.values()))==2,hashes
    record("source.json",{"arms":sources,"benchmark_blob":original_bench[0],"lookup":hashes,"product":os.environ['PRODUCT']})
def order(i):
    return ("stock","fixed") if i%2==0 else ("fixed","stock")
def stats():
    for arm in ["stock","fixed"]:
        lines=[]
        for row in json.loads((out/"paired-bench.json").read_text()):
            if row["arm"]==arm:lines.append(row["line"])
        (out/(arm+".bench")).write_text("\n".join(lines)+"\n")
    tool="2b51abee8530385431b7225198fdd5cd65e78696"
    subprocess.run(["git","fetch","-q","--depth=1","origin",tool],check=True)
    subprocess.run(["git","checkout","-q","--detach",tool],check=True)
    label="@org_golang_x_perf//cmd/benchstat"
    command("benchstat-build",bz+["build",*opts,label])
    binary=output_file("benchstat-output",label)
    p=subprocess.run(gnu+[str(binary),str(out/"stock.bench"),str(out/"fixed.bench")],stdout=subprocess.PIPE,text=True,check=True)
    (out/"benchstat.txt").write_text(p.stdout)
if phase=="prepare":prepare()
elif phase=="benchmark":
    subprocess.run(gnu+["python3",str(root/"pair-bench.py"),str(root)],check=True)
elif phase=="stats":stats()
