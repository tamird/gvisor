"""Compare one sender ACK at two positions on one Linux CPU."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

import qualification_report as report


TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "results"
BENCH = "pkg/tcpip/transport/tcp/sack_scoreboard_test.go"
LABEL = "//pkg/tcpip/transport/tcp:tcp_test"
CASES = {(n, pos) for n in (8, 128) for pos in ("near", "far")}
CONTROLS = {f"TestSACKAccountingQualification/{base}/halves={halves}"
            for base in ("linear", "wrap") for halves in ("true", "false")}
ROW = re.compile(
    r"^BenchmarkSACKAccountingQualification/queue=(8|128)/sack=(near|far)(?:-1)?\s+"
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
           "--test_env=GOMAXPROCS=1", "--test_env=GOGC=100",
           "--nocache_test_results", "--test_timeout=180",
           "--flaky_test_attempts=1", "--runs_per_test=1", "--test_sharding_strategy=disabled",
           "--nobuild_event_json_file_path_conversion", f"--build_event_json_file={events}",
           f"--run_under=/usr/bin/taskset -c {host['cpu']}",
           "--test_arg=-test.run=^TestSACKAccountingQualification$", "--test_arg=-test.v",
           "--test_arg=-test.bench=^BenchmarkSACKAccountingQualification$",
           "--test_arg=-test.benchtime=150ms", "--test_arg=-test.benchmem", LABEL]
    row.update(started=time.time(), args=cmd)
    save(info)
    with (d / "console").open("w") as out:
        result = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT,
                                env={k: os.environ[k] for k in ("PATH", "HOME", "USER")
                                     if k in os.environ} | {"LANG": "C.UTF-8", "TMPDIR": str(TMP)})
    row.update(finished=time.time(), exit=result.returncode)
    if result.returncode:
        console = (d / "console").read_text(errors="replace")
        row["diagnostic_classes"] = [key for key, token in {
            "compile": "GoCompilePkg", "repository": "Error downloading", "timeout": "timed out",
            "undefined": "undefined:", "test_failed": "FAILED", "analysis": "Analysis of target"
        }.items() if token in console]
        row["fixture_error_lines"] = sorted(set(re.findall(r"sack_scoreboard_test[.]go:(\d{1,5}):", console)))[:8]
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
        if not line.startswith("BenchmarkSACKAccountingQualification/") or len(line.split()) == 1:
            continue
        match = ROW.fullmatch(line)
        assert match, line
        n, pos, count, ns, allocated, allocs = match.groups()
        case = (int(n), pos)
        assert case not in seen and int(count) > 1 and float(ns) > 0
        seen.add(case)
        rows.append(line)
    assert seen == CASES, seen
    with (OUT / name).open("a") as out:
        print(*rows, sep="\n", file=out)
    row["rows"] = len(rows)
    xml = ET.parse(OUT / f"{key}.xml")
    leaves = [c for c in xml.iter("testcase") if c.get("name") in CONTROLS]
    assert len(leaves) == 4 and {c.get("name") for c in leaves} == CONTROLS
    assert all(c.get("status", "").lower() not in ("notrun", "suppressed", "skipped")
               and not any(c.find(tag) is not None for tag in ("failure", "error", "skipped"))
               for c in leaves)
    log = (OUT / f"{key}.log").read_text()
    assert all(f"--- PASS: {case} (" in log for case in CONTROLS)
    row["passed_controls"] = sorted(CONTROLS)
    row["verified_at"] = time.time()
    save(info)


def main():
    host = {"arch": platform.machine(), "kernel": platform.release(), "cpu": min(os.sched_getaffinity(0))}
    assert host["arch"] == "x86_64" and shutil.which("taskset") == "/usr/bin/taskset"
    assert blob(BASE, BENCH) == blob(PRODUCT, BENCH)
    assert git("rev-parse", PRODUCT + "^") == BASE
    assert git("diff", "--name-only", BASE, PRODUCT) == "pkg/tcpip/transport/tcp/snd.go"
    assert blob(PRODUCT, "pkg/tcpip/transport/tcp/snd.go") == "8d9e697e7d4b7ebd268b10bd4227f84fee711c06"
    assert git("rev-parse", FIXTURE + "^") == PRODUCT
    assert git("diff", "--name-only", PRODUCT, FIXTURE) == BENCH
    info = {"status": "INCOMPLETE", "base": BASE, "product": PRODUCT, "fixture": FIXTURE,
            "fixture_blob": blob(FIXTURE, BENCH), "host": host, "attempts": [],
            "source_pr": "df000e6d7a2ca32aea30416c12cf257c79806c50",
            "timing_scope": "One SACK-only ACK on an already sent flight with RACK disabled; hot write queue, empty scoreboard per ACK, tiny sender field resets included. No NIC, loss recovery, cumulative retirement or throughput in timing."}
    save(info)
    try:
        for pair in range(1, 9):
            for arm in (("stock", "fixed") if pair % 2 else ("fixed", "stock")):
                measure(pair, arm, host, info)
        assert len(info["attempts"]) == 16 and all(r["rows"] == len(CASES) for r in info["attempts"])
        info.update(status="MEASUREMENTS_PASS")
    finally:
        if any(r.get("exit", 0) != 0 for r in info["attempts"]):
            info["status"] = "FAIL"
        info["verified_at"] = time.time()
        save(info)


if __name__ == "__main__":
    main()
