"""Time sparse-shared allocator checkpoints with two immutable, identical fixtures."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import time

import previous


WORK = Path(os.environ["RUNNER_TEMP"]) / "pgalloc-sparse-checkpoint"
OUT = WORK / "receipt"
PARENT = "deb6b3ea1355ea9798c043cf33bc02804e86ed53"
DRAFT = "b5fd91912d4c10565b3f38d3543dd11f982416ae"
FIXTURES = {
    "parent": ("6f44bed1a753f2f95337cb46ddde312737597d9e", PARENT),
    "draft": ("ad69e80bce8ca2540ac99dc128a18b5755698a11", DRAFT),
}
GO = "pkg/sentry/pgalloc/sparse_checkpoint_benchmark_test.go"
LABEL = "//pkg/sentry/pgalloc:pgalloc_test"
OUTPUTS = Path("bazel-testlogs/pkg/sentry/pgalloc/pgalloc_test")
BENCH = re.compile(r"^BenchmarkSparseSharedCheckpoint/(Save|Load)(?:-1)?\s+64\s+.*ns/op.*$", re.M)
BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g"]
COMMON = [
    "--config=x86_64", "--jobs=4", "--lockfile_mode=off", "--remote_executor=", "--remote_cache=",
    "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local",
    "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
    "--build_tag_filters=", "--nostamp", "--color=no", "--curses=no",
]
TEST = [
    "--strategy=TestRunner=local", "--test_tag_filters=", "--test_sharding_strategy=disabled",
    "--nocache_test_results", "--runs_per_test=1", "--flaky_test_attempts=1", "--test_timeout=600",
    "--test_filter=^$", "--test_env=GOMAXPROCS=1",
    "--test_arg=-test.run=^$", "--test_arg=-test.bench=^BenchmarkSparseSharedCheckpoint$",
    "--test_arg=-test.benchtime=64x", "--test_arg=-test.cpu=1", "--test_arg=-test.count=1",
]
report = {
    "fixtures": FIXTURES, "samples": [], "passed": False, "system": platform.uname()._asdict(),
    "cpu": next(x.split(":", 1)[1].strip() for x in Path("/proc/cpuinfo").read_text().splitlines()
                if x.startswith("model name")),
    "logicalCPUs": sorted(os.sched_getaffinity(0)),
    "pageSize": os.sysconf("SC_PAGE_SIZE"), "benchmarkIterations": 64,
    "extraRefs": 1024, "basePages": 4096, "dirtySharedPages": [0, 4092],
}


def write_report():
    (OUT / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args])


def bazel(name, verb, flags):
    for attempt in range(16):
        cmd = [*BAZEL, verb, *COMMON, *["--distdir=" + str(p) for p in sorted(previous.DIRS)], *flags]
        console = WORK / (name + ".console")
        start = time.monotonic()
        with console.open("wb") as f:
            rc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=5400).returncode
        if rc == 0:
            return
        log = console.read_text(errors="replace")
        (OUT / (name + "-failure-" + str(attempt) + ".log")).write_text(log[-80000:])
        if not previous.recover(log):
            raise RuntimeError((name, rc, round(time.monotonic() - start), log[-1600:]))
    raise RuntimeError("exhausted bounded public dependency recovery for " + name)


def run(arm, block):
    sha, _ = FIXTURES[arm]
    git("checkout", "--quiet", "--detach", sha)
    assert git("rev-parse", "HEAD").decode().strip() == sha
    tag = f"{block:02d}-{arm}"
    bep = WORK / (tag + ".bep")
    bazel(tag, "test", [*TEST, "--build_event_json_file=" + str(bep), LABEL])
    events = []
    for line in bep.open():
        e = json.loads(line)
        if "testResult" in e:
            assert e["id"]["testResult"]["label"] == LABEL
            events.append({"id": e["id"]["testResult"], "result": e["testResult"]})
    assert len(events) == 1, (tag, events)
    result = events[0]["result"]
    assert result["status"] == "PASSED" and not result.get("cachedLocally"), (tag, result)
    assert not result.get("executionInfo", {}).get("cachedRemotely"), (tag, result)
    raw = (OUTPUTS / "test.log").read_bytes()
    assert raw and (OUTPUTS / "test.xml").stat().st_size > 0
    (OUT / (tag + ".log")).write_bytes(raw)
    (OUT / (tag + ".xml")).write_bytes((OUTPUTS / "test.xml").read_bytes())
    text = raw.decode(errors="replace")
    rows = []
    for m in BENCH.finditer(text):
        row = m.group(0).strip()
        assert "checkpoint-B/op" in row and "allocs/op" in row and "B/op" in row, (tag, row)
        rows.append(row)
    assert len(rows) == 2 and {x.split()[0].split("/")[1].split("-")[0] for x in rows} == {"Save", "Load"}, (tag, rows, text[-2500:])
    if block:
        with (OUT / (arm + ".bench")).open("a") as f:
            for row in rows:
                print(row, file=f)
    report["samples"].append({"tag": tag, "commit": sha, "logSHA256": hashlib.sha256(raw).hexdigest(),
                              "rows": rows, "events": events})
    write_report()
    print(tag, *rows, sep="\n", flush=True)


def main():
    assert platform.system() == "Linux" and platform.machine() == "x86_64" and report["pageSize"] == 4096
    previous.OUT = WORK
    previous.REPORT["assets"] = report.setdefault("publicAssets", {})
    assert git("rev-parse", DRAFT + "^").decode().strip() == PARENT
    patches = set()
    for arm, (sha, product) in FIXTURES.items():
        assert git("rev-parse", sha + "^").decode().strip() == product
        assert set(git("diff", "--name-only", product, sha).decode().splitlines()) == {"pkg/sentry/pgalloc/BUILD", GO}
        assert git("rev-parse", sha + ":" + GO).decode().strip() == "283a21f309695cf402feac1b7a09db29458bdaec"
        diff = git("diff", product, sha)
        patches.add(subprocess.check_output(["git", "patch-id", "--stable"], input=diff).split()[0])
        (OUT / (arm + ".patch")).write_bytes(diff)
    assert len(patches) == 1
    report["fixturePatchID"] = patches.pop().decode()
    write_report()
    for arm in FIXTURES:
        run(arm, 0)
    for block in range(1, 9):
        for arm in (("parent", "draft") if block % 2 else ("draft", "parent")):
            run(arm, block)
    # Add only the pinned, previously qualified reporting dependencies, after
    # every measured invocation has finished on its untouched fixture commit.
    old = (WORK / "reporter-module").read_text()
    add = old.split("# Diagnostic-only reporting tool", 1)[1].split("bazel_dep(name = \"rules_go\"", 1)[0]
    add = add.split("\n", 1)[1]
    module = Path("MODULE.bazel")
    module.write_text(module.read_text() + '\nbazel_dep(name = "package_metadata", version = "0.0.5")\n' + add)
    bazel("benchstat", "run", ["@org_golang_x_perf//cmd/benchstat", "--", str(OUT / "parent.bench"), str(OUT / "draft.bench")])
    console = (WORK / "benchstat.console").read_text(errors="replace")
    (OUT / "benchstat.txt").write_text(console)
    assert "sec/op" in console and "Save" in console and "Load" in console and "checkpoint-B/op" in console, console[-1300:]
    report["passed"] = True
    write_report()


try:
    main()
except Exception as exc:
    report["error"] = repr(exc)
    write_report()
    raise
