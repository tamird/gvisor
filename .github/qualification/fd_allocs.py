"""Compare allocations and qualify the original fdbased owner on the final revision."""
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess

work = Path(os.environ["RUNNER_TEMP"]) / "fd-attach-allocs"
sources = {
    "master": "f51a205a8f4afbc5ab41aded070df74951b081ea",
    "parallel": "d4e65b135f3f4e6dbb811a59c38917bdb382d6c3",
    "wg-go": "66eb55f0e3c5a734303e99b6b28baccadba1fb31",
}
owner = "//pkg/tcpip/link/fdbased:fdbased_test"
testfile = Path("pkg/tcpip/link/fdbased/endpoint_test.go")
fixture = (work / "fd_bench.go.inc").read_bytes()
bazel = [str(work / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g"]
common = ["--config=x86_64", "--jobs=3", "--color=no", "--noshow_progress"]
test_flags = ["--strategy=TestRunner=local", "--nocache_test_results", "--test_timeout=240",
              "--test_output=all", "--test_sharding_strategy=disabled", "--test_arg=-test.v",
              "--run_under=/usr/bin/unshare --user --map-root-user --net --"]
report = {"sources": sources, "benchmark_sha256": sha256(fixture).hexdigest(), "cases": {}}
expected = {(str(f), str(p)) for f in (1, 4) for p in (1, 2)}
mandatory = {"TestDetachedReceiveBatch", "TestInjectableAttachment", "TestCloseReleasesStopFDs",
             "TestDeliverPacket", "TestDispatchMalformedIPv4", "TestTCPIPConnectionIDIPv4", "TestTCPIPConnectionIDIPv6"}


def save():
    (work / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args])


def checkout(revision):
    git("checkout", "--quiet", "--detach", revision)
    assert git("rev-parse", "HEAD").decode().strip() == revision


def run(tag, args):
    log = work / (tag + ".log")
    with log.open("w") as stream:
        code = subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT).returncode
    text = log.read_text(errors="replace")
    record = report["cases"][tag] = {"exit": code, "log_sha256": sha256(log.read_bytes()).hexdigest()}
    if code:
        record["diagnostics"] = [line[:600] for line in text.splitlines()
                                 if re.search(r"ERROR:|FAIL:|fatal:|error:|panic:|undefined:|timed out|cgo:|cc1:|gcc:|cannot execute|executable file not found", line, re.I)][:16]
    save()
    print(tag, code, flush=True)
    return text, record


save()
for arm, revision in sources.items():
    checkout(revision)
    original = testfile.read_bytes()
    assert original == git("show", revision + ":" + str(testfile))
    testfile.write_bytes(original + fixture)
    text, record = run(arm, [*bazel, "test", *common, *test_flags,
        "--test_arg=-test.run=^$", "--test_arg=-test.bench=^BenchmarkFDAttachCycle$",
        "--test_arg=-test.benchtime=100x", "--test_arg=-test.count=12", "--test_arg=-test.benchmem", owner])
    rows = []
    counts = Counter()
    for line in text.splitlines():
        m = re.fullmatch(r"BenchmarkFDAttachCycle/fds=(\d+)/processors=(\d+)(?:-\d+)?\s+100\s+[\d.]+ ns/op\s+[\d.]+ B/op\s+[\d.]+ allocs/op\s*", line)
        if m:
            rows.append(line)
            counts[m.groups()] += 1
    (work / (arm + ".bench")).write_text("".join(line + "\n" for line in rows))
    record.update(samples={f"{k[0]}/{k[1]}": n for k, n in counts.items()},
                  verified=record["exit"] == 0 and counts == {k: 12 for k in expected})
    save()
    git("restore", "--source=" + revision, "--worktree", "--", str(testfile))

for mode, extra in (("normal", []), ("race", ["--config=race"])):
    command = [*bazel, "test", *common, *extra, *test_flags, owner]
    if mode == "race":
        image = "gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a"
        # The normal host lacks the project compiler for race builds. This
        # disposable container provides its own network namespace.
        command = ["docker", "run", "--rm", "-v", f"{Path.cwd()}:/src", "-v", f"{work}:/verify",
                   "-w", "/src", "-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory",
                   "-e", "GIT_CONFIG_VALUE_0=/src", "--entrypoint", "/verify/bazel", image,
                   *bazel[1:], "test", *common, *extra, "--spawn_strategy=local",
                   *(flag for flag in test_flags if not flag.startswith("--run_under=")), owner]
    text, record = run(mode, command)
    cases = re.findall(r"(?m)^\s*--- (PASS|FAIL|SKIP): (Test\w+) \(", text)
    record.update(outcomes={name: status for status, name in cases},
                  verified=record["exit"] == 0 and mandatory <= {name for status, name in cases if status == "PASS"})
    save()

# Reuse a pinned standalone reporter without changing any benchmark arm's dependencies.
module = Path("MODULE.bazel")
original = module.read_text()
tool_module = git("show", "2b51abee8530385431b7225198fdd5cd65e78696:MODULE.bazel").decode()
block = tool_module[tool_module.index("go_repository = use_repo_rule"):tool_module.index("bazel_dep(")]
if 'bazel_dep(name = "package_metadata",' not in original:
    block += '\nbazel_dep(name = "package_metadata", version = "0.0.5")\n'
assert 'v0.0.0-20260929162123-406019bb8b68' in block
module.write_text(original + "\n" + block)
target = "@org_golang_x_perf//cmd/benchstat"
_, record = run("reporter", [*bazel, "build", *common, target])
if record["exit"] == 0:
    outputs = subprocess.check_output([*bazel, "cquery", *common, "--output=files", target], text=True).splitlines()
    executable, = [Path(p).resolve() for p in outputs if Path(p).is_file() and os.access(p, os.X_OK)]
    record["executable_sha256"] = sha256(executable.read_bytes()).hexdigest()
    for name, arms in (("benchstat", tuple(sources)), ("previous-benchstat", ("parallel", "wg-go"))):
        with (work / (name + ".txt")).open("w") as out:
            code = subprocess.run([str(executable), *(arm + ".bench" for arm in arms)], cwd=work, stdout=out).returncode
        text = (work / (name + ".txt")).read_text()
        record[name] = code == 0 and "B/op" in text and "allocs/op" in text
    record["verified"] = record["benchstat"] and record["previous-benchstat"]
report["verified"] = all(v.get("verified") for v in report["cases"].values()) and len(report["cases"]) == 6
save()
raise SystemExit(not report["verified"])
