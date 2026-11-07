"""Compare protected mapped-file clears on a single native Linux processor."""

import gzip
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

BASE = "ed70698a7419c55e9b9537d2fb737067b213bdf4"
ERMS = "4b4c6b4793117901deaad118a38f85de17f47ede"
W = Path(os.environ["RUNNER_TEMP"]) / "mapped-clear"
OUT = W / "out"
FILE = W / "backing" / "mapped-file"
PG = "//pkg/sentry/pgalloc:pgalloc_test"
SAFE = "//pkg/safecopy:safecopy_test"
CASES = {PG: ["TestZeroDiskBackedPanicsOnNonzeroUnwritablePage", "TestZeroDiskBackedSkipsZeroUnwritablePage"],
         SAFE: ["TestZeroOutSuccess", "TestZeroOutSegvError", "TestZeroOutBusError"]}
EXTRA = {f"{case}/starting_write_{n}_bytes_before_{sig}" for case, sig in
         (("TestZeroOutSegvError", "SIGSEGV"), ("TestZeroOutBusError", "SIGBUS")) for n in range(33)}
BAZEL = [str(W / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3500m"]
FLAGS = ["-c", "opt", "--config=x86_64", "--jobs=4", "--lockfile_mode=off", "--remote_cache=", "--remote_executor=",
         "--bes_backend=", "--spawn_strategy=linux-sandbox", "--strategy=GoStdlib=processwrapper-sandbox",
         "--strategy=GoStdlibList=processwrapper-sandbox", "--sandbox_default_allow_network=false",
         "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox"]
REPORT = {"base": BASE, "candidate": ERMS, "source_pr": 15181, "flags": FLAGS, "samples": [], "verified": False}
CPU = min(os.sched_getaffinity(0))
SIZE = 64 << 20
ITERS = 32


def save():
    (OUT / "result.json").write_text(json.dumps(REPORT, indent=2, sort_keys=True) + "\n")


def read(*args):
    return subprocess.check_output(args, text=True).strip()


def run(args, name):
    with (W / (name + ".log")).open("w") as output:
        result = subprocess.run(args, stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        lines = (W / (name + ".log")).read_text(errors="replace").splitlines()
        REPORT["first_bazel_error"] = [s[:360] for s in lines if re.search(r"ERROR:|FAIL:|error:|fatal:", s)][:7]
        save()
    return result.returncode


def install_fixture():
    pg = Path("pkg/sentry/pgalloc")
    assert read("git", "hash-object", pg / "BUILD") == "da6e645258e10025ec9fcb3457d52946767e6dc0"
    original = (pg / "BUILD").read_text()
    start, test = original.split('    name = "pgalloc_test",')
    test = test.replace('        "pgalloc_64k_test.go",', '        "disk_clear_benchmark_test.go",\n        "pgalloc_64k_test.go",', 1)
    test = test.replace('        "@org_golang_x_sys//unix:', '        "@org_golang_x_sys//cpu:go_default_library",\n        "@org_golang_x_sys//unix:', 1)
    assert "disk_clear_benchmark_test.go" in test and "//cpu:go_default_library" in test
    (pg / "BUILD").write_text(start + '    name = "pgalloc_test",' + test)
    shutil.copyfile(W / "benchmark.go.inc", pg / "disk_clear_benchmark_test.go")
    shutil.copyfile(W / "safecopy_test.go", "pkg/safecopy/safecopy_test.go")
    assert read("git", "hash-object", "pkg/safecopy/safecopy_test.go") == "8f58ec6d91f7c183852e7b53de0abf9e1264f0fc"
    REPORT["fixture_sha256"] = hashlib.sha256((pg / "disk_clear_benchmark_test.go").read_bytes()).hexdigest()
    REPORT["test_build_sha"] = read("git", "hash-object", pg / "BUILD")


def phase(source, name, controls=False):
    subprocess.run(["git", "switch", "--quiet", "--detach", source], check=True)
    assert read("git", "rev-parse", "HEAD") == source
    assert read("git", "hash-object", "pkg/sentry/pgalloc/pgalloc.go") == "f5c5d16d5c7aebcc813eb222fdd938c163875236"
    owners = list(CASES) if controls else [PG]
    if controls:
        options = ["--test_arg=-test.run=^(" + "|".join(CASES[PG] + CASES[SAFE]) + ")$"]
    else:
        options = ["--test_arg=-test.run=^$", "--test_arg=-test.bench=^BenchmarkZeroDiskBacked64MiB$",
                   "--test_arg=-test.benchtime=" + str(ITERS) + "x", "--test_arg=-test.cpu=1"]
    bep = W / (name + ".bep")
    args = [*BAZEL, "test", *FLAGS, "--nocache_test_results", "--flaky_test_attempts=1",
            "--runs_per_test=1", "--test_sharding_strategy=disabled", "--strategy=TestRunner=linux-sandbox",
            "--test_timeout=180", "--run_under=/usr/bin/taskset -c " + str(CPU),
            "--sandbox_writable_path=" + str(FILE.parent), "--test_env=CLEAR_FILE=" + str(FILE),
            "--test_env=CLEAR_CPU=" + str(CPU), "--test_env=GOMAXPROCS=1", "--test_env=GO_TEST_WRAP_TESTV=1",
            "--test_arg=-test.v", "--build_event_json_file=" + str(bep), *options, *owners]
    rc = run(args, name)
    row = {"source": source, "sample": name, "exit": rc, "owners": {}}
    REPORT["samples"].append(row)
    save()
    for line in bep.open():
        e = json.loads(line)
        if "testResult" not in e:
            continue
        owner = e["id"]["testResult"]["label"]
        r = e["testResult"]
        assert owner in owners and owner not in row["owners"]
        info = r.get("executionInfo", {})
        a = {"status": r["status"], "strategy": info.get("strategy"),
             "cached": bool(r.get("cachedLocally") or info.get("cachedRemotely"))}
        row["owners"][owner] = a
        save()
        files = {}
        for f in r["testActionOutput"]:
            if f["name"] not in ("test.log", "test.xml"):
                continue
            uri = urlparse(f["uri"])
            assert uri.scheme == "file" and not uri.netloc
            data = Path(unquote(uri.path)).read_bytes()
            h = hashlib.sha256(data).hexdigest()
            assert len(data) <= 512 << 10 and (not f.get("digest") or f["digest"] == h)
            assert int(f.get("length", len(data))) == len(data)
            (OUT / "logs" / (name + "-" + owner.split(":")[1] + "-" + f["name"] + ".gz")).write_bytes(gzip.compress(data, mtime=0))
            files[f["name"]] = data
            a[f["name"] + "_sha256"] = h
        save()
        assert a["status"] == "PASSED" and a["strategy"] == "linux-sandbox" and not a["cached"]
        xml = ET.fromstring(files["test.xml"])
        assert (not controls or list(xml.iter("testcase"))) and all(el.tag not in ("error", "failure", "skipped")
               and el.get("status") not in ("notrun", "skipped") and el.get("result") not in ("skipped", "suppressed")
               for el in xml.iter())
        log = files["test.log"].decode().replace("\x16", "")
        if controls:
            required = set(CASES[owner]) | (EXTRA if owner == SAFE else set())
            actual = re.findall(r"(?m)^\s*--- PASS: (\S+) \(", log)
            assert all(actual.count(t) == 1 for t in required), sorted(required - set(actual))
            a["passed_named"] = sorted(required)
        else:
            lines = re.findall(r"(?m)^BenchmarkZeroDiskBacked64MiB(?:-1)?\s+32\s+[\d.]+ ns/op\s+[\d.]+ MB/s[^\n]*", log)
            assert len(lines) == 1, "missing measured benchmark row"
            row["go_row"] = lines[0].strip()
            st = FILE.stat()
            witness = rf"MAPPED_CLEAR cpu={CPU} erms=true dev={st.st_dev} ino={st.st_ino} fstype=([0-9a-f]+) verified={ITERS} size={SIZE}\b"
            hits = re.findall(witness, log)
            assert len(hits) == 1
            row["filesystem_type_hex"] = hits[0]
    save()
    assert rc == 0 and set(row["owners"]) == set(owners)
    return row.get("go_row")


def main():
    assert read("git", "rev-parse", "HEAD^") == ERMS and read("git", "rev-parse", "HEAD^^") == BASE
    subprocess.run(["git", "switch", "--quiet", "--detach", BASE], check=True)
    install_fixture()
    with FILE.open("x+b") as f:
        os.posix_fallocate(f.fileno(), 0, SIZE)
        with mmap.mmap(f.fileno(), SIZE) as m:
            for offset in range(0, SIZE, 1 << 20):
                m[offset:offset+(1 << 20)] = b"\xa5" * (1 << 20)
            m.flush()
    stat = FILE.stat()
    fs = read("findmnt", "-n", "-o", "FSTYPE", "-T", FILE)
    assert fs in ("ext4", "xfs", "btrfs") and os.uname().machine == "x86_64"
    cpuinfo = next(dict(l.split(":",1) for l in c.splitlines() if ":" in l) for c in Path("/proc/cpuinfo").read_text().split("\n\n")
                   if re.search(rf"(?m)^processor\s*:\s*{CPU}$", c))
    cpuinfo = {k.strip():v.strip() for k,v in cpuinfo.items()}
    assert "erms" in cpuinfo["flags"].split()
    REPORT.update(cpu=CPU, model=cpuinfo["model name"], filesystem=fs, shared_device=stat.st_dev, shared_inode=stat.st_ino,
                  bytes=SIZE, clear_iterations=ITERS, kernel=os.uname().release, bazel=read(*BAZEL, "version", "--gnu_format"))
    save()
    for source in (BASE, ERMS):
        phase(source, "controls-" + source[:9], controls=True)
        phase(source, "warmup-" + source[:9])
    samples = {BASE: [], ERMS: []}
    for pair in range(12):
        for source in ((BASE, ERMS) if pair % 2 == 0 else (ERMS, BASE)):
            samples[source].append(phase(source, f"pair-{pair+1:02d}-" + source[:9]))
    for source, rows in samples.items():
        (OUT / (source[:9] + ".txt")).write_text("\n".join(rows) + "\n")
    module = Path("MODULE.bazel")
    tool_module = (W / "benchstat.MODULE").read_text()
    declarations = tool_module[tool_module.index("go_repository ="):tool_module.index('bazel_dep(name = "rules_go",')]
    module.write_text(module.read_text().replace('bazel_dep(name = "rules_go",', declarations + 'bazel_dep(name = "rules_go",', 1))
    target = "@org_golang_x_perf//cmd/benchstat:benchstat"
    assert run([*BAZEL, "build", *FLAGS, target], "benchstat-build") == 0
    paths = read(*BAZEL, "cquery", *FLAGS, "--output=files", target).splitlines()
    exe, = [Path(p).resolve() for p in paths if Path(p).is_file() and os.access(p, os.X_OK)]
    stats = subprocess.check_output([exe, BASE[:9] + ".txt", ERMS[:9] + ".txt"], cwd=OUT, text=True)
    assert "sec/op" in stats and "B/s" in stats and "n=12" in stats
    (OUT / "benchstat.txt").write_text(stats)
    REPORT.update(benchstat_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(), verified=True)
    save()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        REPORT["error"] = f"{type(error).__name__}: {error}"[:450]
        save()
        raise
