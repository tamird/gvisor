import collections
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(os.environ["GO_QA"])
MODE = sys.argv[1]
OUT = ROOT / "receipt" / MODE
OUT.mkdir(parents=True, exist_ok=True)
IMAGE = os.environ["GO_IMAGE"]
NAMES = ["helloworld.go", "typeparam/mdempsky/1.go", "fixedbugs/zz-selection+case[1].go"]
DECOYS = ["syntax/zz-selection+case[1].go", "fixedbugs/zz-selection+case[1].go.tail.go", "fixedbugs/zz-selectionnncase1Xgo.go"]
TERMINAL = re.compile(r"^\s*--- (PASS|SKIP|FAIL): Test/([^ ]+) \(([^)]+)\)", re.M)
ROOTDIR = "/usr/local/go/test/"
OUTCOMES = []


def docker(*args, input=None, check=True):
    return subprocess.run(["docker", *args], input=input, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check)


def save():
    (OUT / "cases.json").write_text(json.dumps(OUTCOMES, indent=2) + "\n")


def run(container, name, cmd, limit=1250):
    started = time.monotonic()
    log = OUT / (name + ".log")
    with log.open("wb") as f:
        try:
            rc = subprocess.run(["docker", "exec", "-e", "GOFLAGS=-v -count=1", container, *cmd],
                                stdout=f, stderr=subprocess.STDOUT, timeout=limit).returncode
        except subprocess.TimeoutExpired:
            rc = 124
    data = log.read_bytes()
    text = data.decode(errors="replace")
    records = [(file, status, duration) for status, file, duration in TERMINAL.findall(text)]
    row = {"name": name, "command": cmd, "exit": rc, "seconds": round(time.monotonic()-started, 3),
           "sha256": hashlib.sha256(data).hexdigest(), "records": records}
    OUTCOMES.append(row)
    save()
    print(name, rc, row["seconds"], dict(collections.Counter(r[1] for r in records)), flush=True)
    return rc, {file: status for file, status, _ in records}, text


def proctor(arm, tests):
    return ["/qa/"+arm, "-runtime=go", "-tests="+",".join(tests)]


def write_file(container, relative, contents):
    docker("exec", "-i", container, "sh", "-c", 'cat > "$1"', "writer", ROOTDIR+relative, input=contents)


def controls(container, runtime):
    # Put malformed decoys next to genuine Go cases without changing the release image.
    write_file(container, NAMES[2], b"// compile\n\npackage p\n")
    for i, name in enumerate(DECOYS):
        write_file(container, name, ("// compile\n\npackage p\nvar _ = QA_UNSELECTED_%d\n" % i).encode())
    helper = docker("exec", container, "sh", "-c", "ls /usr/local/go/test/typeparam/mdempsky/1.dir/*.go | head -1").stdout.decode().strip()
    assert helper.startswith(ROOTDIR + "typeparam/mdempsky/1.dir/")
    original = docker("exec", container, "cat", helper).stdout
    write_file(container, helper.removeprefix(ROOTDIR), original + b"\nvar _ = QA_SELECTED_NESTED_ERROR\n")
    old, old_rows, _ = run(container, runtime+"-stock", proctor("stock", NAMES))
    assert old == 0 and not old_rows, (old, old_rows)
    naive, naive_rows, _ = run(container, runtime+"-no-quotes-only", ["go", "test", "cmd/internal/testdir", "-run", "Test/(helloworld.go|typeparam/mdempsky/1.go)"])
    assert naive == 0 and naive_rows == {"helloworld.go":"PASS"}, naive_rows
    rc, observed, text = run(container, runtime+"-selected-failure", proctor("candidate", NAMES))
    assert rc and observed == {NAMES[0]:"PASS",NAMES[1]:"FAIL",NAMES[2]:"PASS"} and "QA_SELECTED_NESTED_ERROR" in text
    assert "QA_UNSELECTED_" not in text
    write_file(container, helper.removeprefix(ROOTDIR), original)
    rc, observed, _ = run(container, runtime+"-selected-success", proctor("candidate", NAMES))
    assert rc == 0 and observed == dict.fromkeys(NAMES, "PASS")
    rc, observed, text = run(container, runtime+"-selected-decoys", proctor("candidate", DECOYS))
    assert rc != 0 and observed == dict.fromkeys(DECOYS, "FAIL")
    assert all(f"QA_UNSELECTED_{i}" in text for i in range(len(DECOYS)))


def complete(container):
    raw = docker("exec",container,"/qa/candidate","-runtime=go","-list").stdout
    (OUT / "proctor-list.txt").write_bytes(raw)
    disk = sorted(line for line in raw.decode().splitlines() if line.endswith(".go"))
    assert len(set(disk)) == len(disk) == 2525
    assert (ROOT / "go1.22.csv").is_file()
    excluded = [line.split(",")[0] for line in (ROOT / "go1.22.csv").read_text().splitlines()[1:]]
    assert "cmd/internal/testdir:0_1" in excluded and not set(excluded).intersection(disk)
    started = time.monotonic()
    duplicates = []
    unexpected = []
    records = {}
    for i in range(0, len(disk), 50):
        if time.monotonic() - started > 4*3600:
            raise TimeoutError("Four-hour full-suite observation budget elapsed")
        group = disk[i:i+50]
        rc, named, _ = run(container, f"systrap-batch-{i//50:02d}", proctor("candidate",group))
        actual = OUTCOMES[-1]["records"]
        duplicates.extend(file for file, count in collections.Counter(r[0] for r in actual).items() if count > 1)
        duplicates.extend(set(records).intersection(named))
        unexpected.extend(set(named) - set(group))
        records.update(named)
        if rc == 124:
            raise TimeoutError("The proctor hit the 20-minute production batch limit")
    counts = collections.Counter(records.values())
    summary = {"totalFiles":len(disk),"batches":len(OUTCOMES),"seconds":round(time.monotonic()-started,3),
               "outcomes":dict(counts),"missing":sorted(set(disk)-set(records)),"extra":unexpected,"duplicates":duplicates,
               "failed":sorted(n for n,v in records.items() if v=="FAIL"),"skipped":sorted(n for n,v in records.items() if v=="SKIP"),
               "nonzeroBatches":[r["name"] for r in OUTCOMES if r["exit"] != 0]}
    (OUT / "summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    print(json.dumps(summary), flush=True)
    assert not summary["missing"] and not unexpected and not duplicates and not summary["nonzeroBatches"]


for runtime in (["runc", "go-ci"] if MODE == "controls" else ["go-ci"]):
    container = docker("run", "-d", "--network=none", "--runtime="+runtime, "-v",str(ROOT / "bins")+":/qa:ro", IMAGE, "sleep", "infinity").stdout.decode().strip()
    try:
        version = docker("exec",container,"go","version").stdout.decode().strip()
        assert version == "go version go1.22.11 linux/amd64", version
        (OUT/(runtime+"-environment.txt")).write_bytes(docker("exec",container,"sh","-c","go version; nproc; go env GOROOT; uname -a").stdout)
        if MODE == "controls":
            controls(container,runtime)
        else:
            complete(container)
    finally:
        docker("rm", "-f",container,check=False)

