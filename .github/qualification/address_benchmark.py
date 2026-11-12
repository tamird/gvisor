"""Build both sides with Bazel and replay the October address benchmark."""

from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import previous


WORK = Path(os.environ["RUNNER_TEMP"]) / "address-benchmark"
OUT = WORK / "results"
BASE = "2a653cea7b2054a2dc9c6419337d3de8031275d6"
TOPIC = "3aaa8098b633dab610b06e4c6d92122c24037451"
TEST = "//pkg/tcpip:tcpip_test"
TOOL = "@org_golang_x_perf//cmd/benchstat"
FLAGS = ["--config=x86_64", "-c", "opt", "--jobs=4", "--lockfile_mode=off",
         "--remote_executor=", "--remote_cache=", "--bes_backend=",
         "--spawn_strategy=processwrapper-sandbox,local"]
FLAGS += ["--strategy=" + mnemonic + "=processwrapper-sandbox" for mnemonic in ("GoStdlib", "GoStdlibList")]


def bazel(command: str, *args: str) -> str:
    for _ in range(24):
        result = subprocess.run([str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc",
                                 command, *FLAGS, *["--distdir=" + str(d) for d in sorted(previous.DIRS)],
                                 *args], capture_output=True, text=True, timeout=1200)
        print(result.stderr[-2400:], flush=True)
        if result.returncode == 0:
            return result.stdout
        if not previous.recover(result.stderr):
            raise RuntimeError(f"Bazel {command} exited {result.returncode}: {result.stderr[-1800:]}")
    raise RuntimeError("Could not recover pinned public build inputs")


def snapshot_binary(label: str, destination: str) -> None:
    bazel("build", label)
    paths = bazel("cquery", "--output=files", label).strip().splitlines()
    [path] = paths
    binary = Path(path)
    size = binary.stat().st_size
    assert size > 10000, (path, size)
    shutil.copy2(binary, WORK / destination)


def build_benchmark(revision: str, name: str) -> None:
    subprocess.run(["git", "checkout", "--detach", revision], check=True)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == revision
    fixture = Path("pkg/tcpip/address_bench_test.go")
    assert not fixture.exists()
    shutil.copyfile(WORK / fixture.name, fixture)
    build = Path("pkg/tcpip/BUILD")
    old = build.read_text()
    anchor = '        "errors_test.go",'
    assert old.count(anchor) == 1, build
    build.write_text(old.replace(anchor, '        "address_bench_test.go",\n' + anchor))
    snapshot_binary(TEST, name)
    subprocess.run(["git", "restore", "--", str(build)], check=True)
    fixture.unlink()


def benchmark() -> None:
    cpus = sorted(os.sched_getaffinity(0))
    cpu = cpus[-1]
    os.sched_setaffinity(0, {cpu})
    assert os.sched_getaffinity(0) == {cpu}
    env = dict(os.environ, GOMAXPROCS="1")
    times = 12
    families = ("IPv4", "IPv6", "MappedIPv4")
    ops = ("Construct", "CopyBytes", "MapLookup")
    expected = {a + "/" + b for a in families for b in ops}
    binary_opts = ["-test.run=^$", "-test.bench=^BenchmarkAddress$", "-test.benchmem",
                   "-test.benchtime=500ms", "-test.cpu=1", "-test.count=1"]
    order = []
    for group in range(-1, times):
        names = ["parent", "change"] if group % 2 == 0 else ["change", "parent"]
        for name in names:
            run = subprocess.run([str(WORK / name), *binary_opts], env=env,
                                 capture_output=True, text=True, check=True, timeout=80)
            cases = re.findall(r"(?m)^BenchmarkAddress/([^\s]+)\s+[0-9]+\s+[0-9.]+ ns/op", run.stdout)
            assert Counter(cases) == Counter(expected), (name, group, cases, run.stdout)
            if group < 0:
                continue
            with (OUT / name).open("a") as handle:
                handle.write(run.stdout)
            order.append([group + 1, name])
        print("finished sample pair", group + 1, flush=True)
    (OUT / "method.json").write_text(json.dumps({
        "base": BASE, "topic": TOPIC, "go_fixture": "git:f0a0dff5f:pkg/tcpip/address_bench_test.go",
        "cpus_available": cpus, "cpu_used": cpu, "samples_per_case": times,
        "warmup_pairs_discarded": 1, "run_order": order, "GOMAXPROCS": env["GOMAXPROCS"],
        "benchmark_args": binary_opts, "bazel_flags": FLAGS, "kernel": os.uname().release,
        "cpuinfo": next(s.split(":")[1].strip() for s in Path("/proc/cpuinfo").read_text().splitlines()
                        if s.startswith("model name")),
    }, indent=2) + "\n")


def build_benchstat() -> None:
    # Use October's two pinned Go repositories and checksums. The graph used for
    # the measurements above is the production graph for each revision.
    module = Path("MODULE.bazel")
    original = module.read_text()
    anchor = '    "org_golang_x_sync",'
    assert original.count(anchor) == 1, module
    module.write_text(original.replace(anchor, '    "org_golang_x_perf",\n' + anchor)
                      + "\n" + (WORK / "benchstat.star").read_text())
    snapshot_binary(TOOL, "benchstat")
    subprocess.run(["git", "restore", "--", str(module)], check=True)


previous.OUT = WORK
build_benchmark(BASE, "parent")
build_benchmark(TOPIC, "change")
benchmark()
build_benchstat()
table = subprocess.check_output([str(WORK / "benchstat"), "parent", "change"], cwd=OUT, text=True)
(OUT / "benchstat.txt").write_text(table)
print(table, flush=True)
