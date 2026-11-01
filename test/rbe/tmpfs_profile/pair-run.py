"""Compare independently warmed slice loads on the fixed native CPU."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

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
    line=[x for x in text.splitlines() if re.search(r"^[^ :]+\.go:\d+:|^ERROR:|^FAILED:|Build did NOT complete successfully|Invocation ID:|loading.*failed",x)]
    record(name+"-command.json",{"exit":p.returncode,"start":start,"end":end,"diagnostics":[v[:360] for v in line[-10:] if not re.search(r"remote_header|api.key|authorization|cookie=",v,re.I)]})
    if p.returncode and not allow_failure:
        raise RuntimeError(f"{name} exited {p.returncode}; see its redacted command receipt")
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
def benchstat(stock, fixed, report):
    tool="2b51abee8530385431b7225198fdd5cd65e78696"
    subprocess.run(["git","fetch","-q","--depth=1","origin",tool],check=True)
    subprocess.run(["git","checkout","-q","--detach",tool],check=True)
    label="@org_golang_x_perf//cmd/benchstat"
    command("benchstat-build",bz+["build",*opts,label])
    binary=output_file("benchstat-output",label)
    p=subprocess.run(gnu+[str(binary),str(out/stock),str(out/fixed)],stdout=subprocess.PIPE,text=True,check=True)
    (out/report).write_text(p.stdout)
def prepare():
    files = {}
    for arm in sources:
        select(arm)
        owner = "//pkg/state/tests:tests_test"
        command(arm + "-build", bz + ["build", *opts, owner])
        exe = output_file(arm + "-path", owner)
        dst = str(root / arm)
        subprocess.run(gnu + ["cp", str(exe), dst], check=True)
        files[arm] = subprocess.check_output(gnu + ["sha256sum", dst], text=True).split()[0]
        record("source.json", {"arms": sources, "binaries": files, "owner": owner})
    assert len(set(files.values())) == 2, files


def measure():
    cpu = subprocess.check_output(gnu + ["python3", "-c", "import os;print(min(os.sched_getaffinity(0)))"], text=True).strip()
    assert cpu.isdigit(), cpu
    rows = []
    sizes = [1024, 8191, 8192]
    for pair in range(16):
        for size in sizes[pair % 3:] + sizes[:pair % 3]:
            for arm in (("stock", "fixed") if pair % 2 == 0 else ("fixed", "stock")):
                case = "BenchmarkCallbackPoolBoundary/" + str(size)
                args = gnu + ["env", "-u", "XML_OUTPUT_FILE", "GOMAXPROCS=1", "GO_TEST_WRAP=0", "taskset", "-c", cpu, str(root / arm), "-test.cpu=1", "-test.run=^$", "-test.bench=^BenchmarkCallbackPoolBoundary$/^" + str(size) + "$", "-test.benchtime=32x", "-test.benchmem"]
                start = time.time_ns()
                p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
                lines = [line for line in p.stdout.splitlines() if line.startswith(case) or "decoder used" in line or "decoder releases" in line or line.startswith("--- FAIL")]
                row = {"pair": pair, "size": size, "arm": arm, "start": start, "end": time.time_ns(), "cpu": int(cpu), "exit": p.returncode, "lines": lines}
                rows.append(row)
                record("rows.json", rows)
                assert p.returncode == 0 and re.search(r"^PASS$", p.stdout, re.M) and len(lines) == 1, row
                line, = lines
                name, count, *fields = line.split()
                metrics = {unit: float(n) for n, unit in zip(fields[::2], fields[1::2])}
                assert name == case and count == "32" and all(metrics[k] > 0 for k in ("ns/op", "B/op", "allocs/op")), row
                assert metrics["callbacks"] == size + 1 and metrics["warm-pool-puts"] == (0 if arm == "fixed" and size == 8192 else 4), row
    assert len(rows) == 96
    for arm in sources:
        (out / (arm + ".bench")).write_text("\n".join(r["lines"][0] for r in rows if r["arm"] == arm) + "\n")
    record("measure.json", {"rows": len(rows), "cpu": int(cpu), "sizes": sizes, "finishNs": time.time_ns()})


if phase == "prepare":
    prepare()
elif phase == "measure":
    measure()
elif phase == "stats":
    benchstat("stock.bench", "fixed.bench", "benchstat.txt")
    record("verdict.json", {"pass": True, "base": os.environ["BASE"], "candidate": os.environ["PRODUCT"], "finishNs": time.time_ns()})
else:
    raise ValueError(phase)
