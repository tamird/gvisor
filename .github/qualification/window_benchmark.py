"""Compare both exact encoders with the same fixture and pinned TestRunner CPU."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time

import qualification_report as report


TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "results"
BENCH = "pkg/state/tests/bench_test.go"
LABEL = "//pkg/state/tests:tests_test"
CASES = {(size, order) for size in (128, 512, 2048) for order in ("narrow", "wide")}
ROW = re.compile(
    r"^BenchmarkSharedSliceSave/windows=(128|512|2048)/(narrow|wide)(?:-1)?\s+"
    r"(\d+)\s+([0-9.]+) ns/op\s+(\d+) B/op\s+(\d+) allocs/op\s*$"
)
BASE, PRODUCT, FIXTURE = (os.environ[name] for name in ("BASE", "PRODUCT", "FIXTURE"))


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def blob(source, path):
    return git("rev-parse", f"{source}:{path}")


def save(info):
    report.write_json(OUT / "verdict.json", info)


def measure(pair, name, host, info):
    key = f"pair-{pair}-{name}"
    source = BASE if name == "stock" else FIXTURE
    row = {"pair": pair, "arm": name, "source": source}
    info["attempts"].append(row)
    save(info)
    # The remote worktree is disposable. Only the fixture is overlaid on stock.
    git("restore", "--worktree", "--", BENCH)
    git("checkout", "--quiet", "--detach", source)
    if name == "stock":
        data = subprocess.check_output(["git", "show", f"{FIXTURE}:{BENCH}"])
        Path(BENCH).write_bytes(data)
    assert git("rev-parse", "HEAD") == source
    assert git("hash-object", BENCH) == info["fixture_blob"]
    assert git("diff", "--name-only", "HEAD") == (BENCH if name == "stock" else "")
    d = TMP / key
    d.mkdir()
    events = d / "raw.jsonl"
    cmd = ["bazelisk", "test", "-c", "opt", "--jobs=4", "--remote_cache=", "--remote_executor=",
           "--test_env=GOMAXPROCS=1", "--test_env=GOGC=100", "--nocache_test_results",
           "--flaky_test_attempts=1", "--runs_per_test=1", "--test_sharding_strategy=disabled",
           "--nobuild_event_json_file_path_conversion", f"--build_event_json_file={events}",
           f"--run_under=/usr/bin/taskset -c {host['cpu']}",
           "--test_arg=-test.run=^$", "--test_arg=-test.bench=^BenchmarkSharedSliceSave$",
           "--test_arg=-test.benchtime=300ms", "--test_arg=-test.benchmem", LABEL]
    row.update(started=time.time(), args=cmd)
    save(info)
    with (d / "console").open("w") as out:
        result = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT)
    row.update(finished=time.time(), exit=result.returncode)
    if result.returncode:
        errors = [s for s in (d / "console").read_text(errors="replace").splitlines()
                  if re.search(r"^ERROR:|^FATAL:|undefined:|syntax error:", s)]
        row["diagnostic"] = [s[:400] for s in errors[:6]]
    save(info)
    capture = report.capture(events, d / "events.jsonl", Path("bazel-out"), d / "reports")
    assert not capture["errors"], capture["errors"]
    observations = [e for l in events.read_text().splitlines() if "testResult" in (e := json.loads(l))["id"]]
    assert len(observations) == 1
    event, = observations
    attempt, payload = event["id"]["testResult"], event["testResult"]
    row.update(identity=attempt, status=payload["status"], runner=payload.get("executionInfo", {}).get("strategy"))
    for ext in ("log", "xml"):
        path, = (d / "reports").rglob("test." + ext)
        data = path.read_bytes()
        (OUT / f"{key}.{ext}").write_bytes(data)
        row[ext + "_sha256"] = hashlib.sha256(data).hexdigest()
    save(info)
    assert attempt["label"] == LABEL and all(attempt.get(k, 1) == 1 for k in ("run", "shard", "attempt"))
    assert row["runner"] in ("linux-sandbox", "processwrapper-sandbox", "local", "standalone"), row["runner"]
    assert not payload.get("cachedLocally") and not payload.get("executionInfo", {}).get("cachedRemotely")
    assert result.returncode == 0 and payload["status"] == "PASSED"
    rows = []
    seen = set()
    for line in (OUT / f"{key}.log").read_text().splitlines():
        if not line.startswith("BenchmarkSharedSliceSave/") or len(line.split()) == 1:
            continue
        match = ROW.fullmatch(line)
        assert match, line
        size, order, count, ns, allocated, allocs = match.groups()
        assert (int(size), order) not in seen and int(count) > 1 and float(ns) > 0
        seen.add((int(size), order))
        rows.append(line)
    assert seen == CASES, seen
    with (OUT / name).open("a") as out:
        print(*rows, sep="\n", file=out)
    row["rows"] = len(rows)
    save(info)


def main():
    host = {"arch": platform.machine(), "kernel": platform.release(), "cpu": min(os.sched_getaffinity(0))}
    assert host["arch"] == "x86_64" and shutil.which("taskset") == "/usr/bin/taskset"
    assert blob(BASE, BENCH) == blob(PRODUCT, BENCH)
    info = {"status": "INCOMPLETE", "base": BASE, "product": PRODUCT, "fixture": FIXTURE,
            "fixture_blob": blob(FIXTURE, BENCH), "host": host, "attempts": []}
    save(info)
    for pair in range(1, 9):
        for arm in (("stock", "fixed") if pair % 2 else ("fixed", "stock")):
            measure(pair, arm, host, info)
    assert len(info["attempts"]) == 16 and all(r["rows"] == len(CASES) for r in info["attempts"])
    info.update(status="PASS", verified_at=time.time())
    save(info)


if __name__ == "__main__":
    main()
