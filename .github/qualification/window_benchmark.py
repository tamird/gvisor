"""Compare packet reads on one Linux CPU, keeping header correctness separate."""
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
BENCH = "pkg/tcpip/stack/packet_buffer_test.go"
LABEL = "//pkg/tcpip/stack:stack_test"
CASES = {(size, views, headers, read) for size in (1460, 65536)
         for views in (1, 4, 16) for headers in (0, 54) for read in (64, size)}
ROW = re.compile(
    r"^BenchmarkPacketReadQualification/size=(1460|65536)/views=(1|4|16)/headers=(0|54)/read=(64|1460|65536)(?:-1)?\s+"
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
    source = BASE if name == "author" else PRODUCT
    row = {"pair": pair, "arm": name, "source": source}
    info["attempts"].append(row)
    save(info)
    # The remote worktree is disposable. Both sources run the identical fixture.
    git("restore", "--worktree", "--", BENCH)
    git("checkout", "--quiet", "--detach", source)
    data = subprocess.check_output(["git", "show", f"{FIXTURE}:{BENCH}"])
    Path(BENCH).write_bytes(data)
    assert git("rev-parse", "HEAD") == source
    assert git("hash-object", BENCH) == info["fixture_blob"]
    assert git("diff", "--name-only", "HEAD") == BENCH
    d = TMP / key
    d.mkdir()
    events = d / "raw.jsonl"
    cmd = ["bazelisk", "test", "-c", "opt", "--jobs=4", "--remote_cache=", "--remote_executor=",
           "--test_env=GOMAXPROCS=1", "--test_env=GOGC=100",
           "--nocache_test_results", "--test_timeout=180",
           "--flaky_test_attempts=1", "--runs_per_test=1", "--test_sharding_strategy=disabled",
           "--nobuild_event_json_file_path_conversion", f"--build_event_json_file={events}",
           f"--run_under=/usr/bin/taskset -c {host['cpu']}",
           "--test_arg=-test.run=^TestPacketReadQualification$", "--test_arg=-test.v",
           "--test_arg=-test.bench=^BenchmarkPacketReadQualification$",
           "--test_arg=-test.benchtime=150ms", "--test_arg=-test.benchmem", LABEL]
    row.update(started=time.time(), args=cmd)
    save(info)
    with (d / "console").open("w") as out:
        result = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT,
                                env={k: os.environ[k] for k in ("PATH", "HOME", "USER")
                                     if k in os.environ} | {"LANG": "C.UTF-8", "TMPDIR": str(TMP)})
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
    row["test_timing"] = {k: payload[k] for k in ("testAttemptStartMillis", "testAttemptDurationMillis", "testAttemptStart", "testAttemptDuration") if k in payload}
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
        if not line.startswith("BenchmarkPacketReadQualification/") or len(line.split()) == 1:
            continue
        match = ROW.fullmatch(line)
        assert match, line
        size, views, headers, read, count, ns, allocated, allocs = match.groups()
        case = tuple(map(int, (size, views, headers, read)))
        assert case not in seen and int(count) > 1 and float(ns) > 0
        seen.add(case)
        rows.append(line)
    assert seen == CASES, seen
    with (OUT / name).open("a") as out:
        print(*rows, sep="\n", file=out)
    row["rows"] = len(rows)
    observations = []
    for line in (OUT / f"{key}.log").read_text().splitlines():
        if "PACKET_READ " not in line:
            continue
        match = re.search(r"PACKET_READ size=(\d+) views=(\d+) headers=(\d+) read=(\d+) peek=(true|false) payload=true retained_headers=(true|false)$", line)
        assert match, line
        size, views, headers, read, peek, preserved = match.groups()
        case = tuple(map(int, (size, views, headers, read)))
        assert case in CASES
        assert preserved == "true"
        observations.append({"case": case, "peek": peek == "true", "headers_preserved": preserved == "true"})
    assert len(observations) == 48 and len({(*r["case"], r["peek"]) for r in observations}) == 48
    row["correctness"] = observations
    row["verified_at"] = time.time()
    save(info)


def main():
    host = {"arch": platform.machine(), "kernel": platform.release(), "cpu": min(os.sched_getaffinity(0))}
    assert host["arch"] == "x86_64" and shutil.which("taskset") == "/usr/bin/taskset"
    assert blob(BASE, BENCH) == blob(PRODUCT, BENCH)
    assert git("rev-parse", PRODUCT + "^") == BASE
    assert set(git("diff", "--name-only", BASE, PRODUCT).splitlines()) == {
        "pkg/buffer/buffer.go", "pkg/buffer/buffer_test.go", "pkg/tcpip/stack/packet_buffer.go",
    }
    assert git("rev-parse", FIXTURE + "^") == PRODUCT
    info = {"status": "INCOMPLETE", "base": BASE, "product": PRODUCT, "fixture": FIXTURE,
            "fixture_blob": blob(FIXTURE, BENCH), "host": host, "attempts": [],
            "source_pr": BASE,
            "timing_scope": "Hot shared payload ReadTo only; packet cloning and release excluded; both arms preserve all headers"}
    save(info)
    try:
        for pair in range(1, 9):
            for arm in (("author", "prefix") if pair % 2 else ("prefix", "author")):
                measure(pair, arm, host, info)
        assert len(info["attempts"]) == 16 and all(r["rows"] == len(CASES) for r in info["attempts"])
        info.update(status="PASS")
    finally:
        if any(r.get("exit", 0) != 0 for r in info["attempts"]):
            info["status"] = "FAIL"
        info["verified_at"] = time.time()
        save(info)


if __name__ == "__main__":
    main()
