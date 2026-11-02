import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "records"
docker = ["docker", "exec", "tcp-compiler"]
bazel = [*docker, "bazel", f"--bazelrc={tmp}/tcp.bazelrc"]
opts = ["--config=x86_64", "--jobs=64", "--remote_download_outputs=toplevel"]
sources = {"stock": os.environ["BASE"], "fixed": os.environ["FIXED"]}
pairs = 16
controls = {
    "tcp_socket": ["NoLingerOnProcessExit", "CloseNonConnectedLingerOption"],
    "tcp_loopback": ["CloseWithLingerOptionInFinWait2", "ResetWithSoLingerZeroTimeoutOption"],
}


def record(name, data):
    (out / name).write_text(json.dumps(data, indent=2) + "\n")


def build_outputs(labels):
    subprocess.run([*bazel, "build", *opts, *labels.values()], check=True)
    for name, label in labels.items():
        files = subprocess.check_output(
            [*bazel, "cquery", *opts, "--output=files", label], text=True
        ).splitlines()
        path, = files
        subprocess.run([*docker, "install", "-m", "755", path, str(tmp / name)], check=True)


def build():
    build_outputs({
        "close_sample": "//test/rbe/socket_close_waiter:close_sample",
        "tcp_socket": "//test/syscalls/linux:tcp_socket_test",
        "tcp_loopback": "//test/syscalls/linux:socket_ip_tcp_generic_loopback_test",
    })
    digests = {}
    for arm, source in sources.items():
        subprocess.run(["git", "checkout", "--quiet", "--detach", source], check=True)
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
        subprocess.run([*bazel, "build", *opts, "//:release"], check=True)
        subprocess.run([*docker, "cp", "-aL", "bazel-bin/release", str(tmp / arm)], check=True)
        digests[arm] = {}
        for binary in sorted((tmp / arm).rglob("*")):
            if binary.is_file():
                with binary.open("rb") as f:
                    digests[arm][str(binary.relative_to(tmp / arm))] = hashlib.file_digest(f, "sha256").hexdigest()
    record("sources.json", {"commits": sources, "releaseSHA256": digests})
    assert set(digests["stock"]) == set(digests["fixed"]) and len(digests["stock"]) == 8
    sentry = "gvisor-bin/gvisor_sentry"
    assert digests["stock"][sentry] != digests["fixed"][sentry]
    benchstat_source = "2b51abee8530385431b7225198fdd5cd65e78696"
    subprocess.run(["git", "fetch", "--quiet", "--depth=1", "origin", benchstat_source], check=True)
    subprocess.run(["git", "checkout", "--quiet", "--detach", benchstat_source], check=True)
    build_outputs({"benchstat": "@org_golang_x_perf//cmd/benchstat"})


def guest(arm, program, *args):
    command = [str(tmp / program), *args]
    if arm != "linux":
        command = ["sudo", str(tmp / arm / "runsc"), "--platform=systrap", "--network=none",
                   f"--root={tmp}/root-{arm}", "do", *command]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=360)
    if result.returncode:
        (out / f"{arm}-{program}-failure.txt").write_text(result.stdout)
        result.check_returncode()
    return result.stdout


def check_controls():
    observed = {}
    for arm in ["linux", *sources]:
        observed[arm] = []
        for program, names in controls.items():
            selection = ":".join(f"*.{name}/*" for name in names)
            log = guest(arm, program, f"--gtest_filter={selection}", "--gtest_color=no")
            (out / f"{arm}-{program}.txt").write_text(log)
            started = re.findall(r"^\[ RUN +\] (\S+)", log, re.M)
            passed = re.findall(r"^\[ +OK +\] (\S+)", log, re.M)
            assert sorted(started) == sorted(passed) and len(started) == len(set(started)), log
            for name in names:
                assert any(f".{name}/" in case for case in passed), log
            observed[arm].extend(sorted(passed))
        assert observed[arm] == observed["linux"]
    record("controls.json", observed)


def measure():
    for i in range(pairs):
        for arm in (["stock", "fixed"] if i % 2 == 0 else ["fixed", "stock"]):
            sample = guest(arm, "close_sample")
            rows = re.findall(r"^BenchmarkClose/(tcp|udp|unix) 32768 ([\d.]+) ns/op$", sample, re.M)
            assert [name for name, _ in rows] == ["tcp", "udp", "unix"], sample
            assert all(float(ns) > 0 for _, ns in rows)
            with (out / f"{arm}.bench").open("a") as f:
                for name, ns in rows:
                    f.write(f"BenchmarkClose/{name} 32768 {ns} ns/op\n")
    record("finished.json", {"executionFinishedNs": time.time_ns(), "pairs": pairs})
    with (out / "benchstat.txt").open("w") as report:
        subprocess.run([str(tmp / "benchstat"), "stock.bench", "fixed.bench"],
                       cwd=out, check=True, stdout=report)
    print((out / "benchstat.txt").read_text())


def verdict():
    steps = json.loads(os.environ["OUTCOMES"])
    stages = {name: steps.get(name, {}).get("outcome", "missing")
              for name in ["build", "controls", "timing"]}
    missing = [name for name in ["sources.json", "controls.json", "finished.json", "benchstat.txt"]
               if not (out / name).is_file()]
    counts = {arm: len((out / f"{arm}.bench").read_text().splitlines())
              if (out / f"{arm}.bench").exists() else 0 for arm in sources}
    passed = all(status == "success" for status in stages.values()) and not missing
    passed = passed and all(count == 3 * pairs for count in counts.values())
    record("verdict.json", {"pass": passed, "sources": sources, "stages": stages, "missing": missing,
                            "expectedBenchRowsPerArm": 3 * pairs, "executedBenchRows": counts,
                            "expectedControlGroups": controls, "verifiedNs": time.time_ns()})
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    {"build": build, "controls": check_controls, "timing": measure, "verdict": verdict}[sys.argv[1]]()
