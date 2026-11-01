"""Run the existing native tmpfs lifecycle on three immutable GC sources."""
import datetime
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse
from xml.etree import ElementTree

ROOT = Path(os.environ["RUNNER_TEMP"]) / "gc-save"
OUT = ROOT / "out"
SOURCES = {
    "baseline": "0bbcbf9de7626fcecba97f37723dce80b435d73d",
    "reference": "79637cb45ac762b3a5de6b08d0c2e6067b16ca50",
    "proposed": "5068ecb10e0ca383cc00791cc4a655b7a90a626f",
}
TARGET = "//runsc/container:container_test"
CASE = "TestGCReferenceSave"
GNU = ["docker", "exec", "gc-coordinator"]
BAZEL = GNU + ["bazel", "--bazelrc=" + str(ROOT / "remote.bazelrc")]
OPTIONS = ["--config=x86_64", "--jobs=24", "--remote_download_outputs=toplevel", "--noremote_upload_local_results", "--build_event_json_file_path_conversion=false"]
PERMUTATIONS = list(itertools.permutations(SOURCES))
RECEIPT = {"status": "INCOMPLETE", "sources": SOURCES, "rounds": 12, "warmups_per_arm": 1, "rows": [], "commands": [], "fixture_sha256": hashlib.sha256((ROOT / "fixture.go").read_bytes()).hexdigest()}


RECEIPT["plan"] = {
    "reference_original": "e4184f4509ad634618c61aa7bb854b5cc3ccf6db",
    "image": os.environ["GNU"],
    "order": PERMUTATIONS * 2,
    "workload": {"files": 100000, "depth": 100, "file_bytes": 0, "engine": "wire", "platform": "systrap", "memory_bytes": 2 << 30, "sandbox_cpu_quota": "max", "idle_seconds": 2},
    "stop": "12 complete triplets after one warmup per arm, or first setup/functional failure; no timing outlier exclusions",
    "memory_scope": "lifetime waitid ru_maxrss; separate pre-save VmRSS/VmHWM and sampled save-through-exit RSS",
    "scope": "save only; every archive restored and 100000 files verified; not the published C4/COS environment or HTTP readiness",
}

def save():
    OUT.mkdir(exist_ok=True)
    (OUT / "receipt.json").write_text(json.dumps(RECEIPT, indent=2) + "\n")

def run(name, args):
    start = time.time_ns()
    with (ROOT / (name + ".log")).open("wb") as stream:
        proc = subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT)
    end = time.time_ns()
    RECEIPT["commands"].append({"phase": name, "exit": proc.returncode, "start_ns": start, "end_ns": end})
    save()
    return proc.returncode

def select(arm):
    subprocess.run(["git", "restore", "--", "runsc/container/BUILD"], check=True)
    subprocess.run(["git", "checkout", "--quiet", "--detach", SOURCES[arm]], check=True)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCES[arm]
    subprocess.run(["git", "diff", "--exit-code", "--quiet"], check=True)
    path = Path("runsc/container/BUILD")
    original = path.read_text()
    line = '        "container_save_restore_test.go",\n'
    assert original.count(line) == 1
    path.write_text(original.replace(line, line + '        "gc_reference_test.go",\n'))
    Path("runsc/container/gc_reference_test.go").write_bytes((ROOT / "fixture.go").read_bytes())
    subprocess.run(["git", "diff", "--exit-code", "--quiet", "--", ".", ":!runsc/container/BUILD"], check=True)
    assert hashlib.sha256(Path("runsc/container/gc_reference_test.go").read_bytes()).hexdigest() == RECEIPT["fixture_sha256"]
    subprocess.run(["git", "diff", "--exit-code", SOURCES["baseline"], SOURCES[arm], "--", "test/cmd/test_app"], check=True)

def finish_time(event):
    if event.get("finishTimeMillis"):
        return int(event["finishTimeMillis"]) * 1_000_000
    return int(datetime.datetime.fromisoformat(event["finishTime"].replace("Z", "+00:00")).timestamp() * 1e9)

def observe(arm, number):
    select(arm)
    name = f"{number:02}-{arm}"
    bep = ROOT / (name + ".bep")
    code = run(name, BAZEL + ["test", *OPTIONS, "--strategy=TestRunner=local", "--test_filter=^" + CASE + "$", "--test_arg=-test.v", "--test_sharding_strategy=disabled", "--test_timeout=240", "--nocache_test_results", "--build_event_json_file=" + str(bep), TARGET])
    record = {"arm": arm, "round": number, "source": SOURCES[arm], "bazel_exit": code, "status": "FAIL" if code else "INCOMPLETE"}
    RECEIPT["rows"].append(record)
    save()
    events = [json.loads(line) for line in bep.read_text().splitlines()]
    results = [e for e in events if "testResult" in e]
    summaries = [e for e in events if "testSummary" in e]
    finished = [e["finished"] for e in events if "finished" in e]
    assert len(finished) == 1
    record["execution_finished_ns"] = finish_time(finished[0])
    record["result_count"] = len(results)
    record["summaries"] = [{"label": e["id"]["testSummary"]["label"], "status": e["testSummary"]["overallStatus"]} for e in summaries]
    save()
    assert len(results) == len(summaries) == 1
    result = results[0]
    assert result["id"]["testResult"]["label"] == summaries[0]["id"]["testSummary"]["label"] == TARGET
    detail = result["testResult"]
    record["test_status"] = detail["status"]
    if detail["status"] not in ("PASSED", "NO_STATUS"):
        record["status"] = "FAIL"
    save()
    record["execution"] = {key: detail.get("executionInfo", {}).get(key) for key in ("strategy", "cachedRemotely", "exitCode")}
    record["cached_locally"] = detail.get("cachedLocally", False)
    files = {f["name"]: f for f in detail.get("testActionOutput", [])}
    for filename in ["test.xml", "test.log"]:
        uri = urllib.parse.urlparse(files[filename]["uri"])
        assert uri.scheme == "file" and not uri.netloc
        path = urllib.parse.unquote(uri.path)
        assert path.endswith("/testlogs/runsc/container/container_test/" + filename), path
        raw = subprocess.check_output(GNU + ["cat", path])
        record[filename + "_sha256"] = hashlib.sha256(raw).hexdigest()
        if filename == "test.xml":
            cases = list(ElementTree.fromstring(raw).iter("testcase"))
            record["cases"] = [{"name": c.get("name"), "failed": any(c.find(k) is not None for k in ("error", "failure")), "skipped": c.find("skipped") is not None or c.get("status") in ("notrun", "suppressed") or c.get("result") == "suppressed"} for c in cases]
        else:
            log = raw.decode(errors="replace")
            measured = re.findall(r"GC-SAVE-MEASURED (\{[^\n]+\})", log)
            rows = re.findall(r"GC-SAVE-ROW (\{[^\n]+\})", log)
            record["measured"] = [json.loads(row) for row in measured]
            record["observations"] = [json.loads(row) for row in rows]
            # Fixture diagnostics only; remote coordinator logs remain private.
            record["diagnostics"] = [line[:700] for line in log.splitlines() if re.search(r"gc_reference_test.go:\d+:", line) and "GC-SAVE-" not in line][-8:]
    save()
    assert code == 0 and detail["status"] == "PASSED" and record["summaries"][0]["status"] == "PASSED", record["test_status"]
    assert not record["cached_locally"] and not record["execution"]["cachedRemotely"]
    assert record["execution"]["strategy"] in ("local", "standalone"), record["execution"]
    assert record["cases"] == [{"name": CASE, "failed": False, "skipped": False}], record["cases"]
    assert len(record["observations"]) == len(record["measured"]) == 1
    row = record["observations"][0]
    assert all(row[key] == value for key, value in record["measured"][0].items())
    assert row["files_verified"] == 100000 and row["memory_limit"] == 2 << 30
    assert row["checkpoint_exit_ns"] >= row["checkpoint_ns"] > 0
    assert row["lifetime_maxrss"] >= max(row["before_hwm"], row["observed_save_exit_rss"])
    assert row["samples"] > 0 and row["coordinator_cpus"] > 0 and row["direct_child"] is True
    ceiling = float(row["coordinator_cpus"])
    for value in row["cpu_quotas"]:
        quota, period = value.split()
        assert int(period) > 0
        if quota != "max":
            assert int(quota) > 0
            ceiling = min(ceiling, int(quota) / int(period))
    record["ancestor_and_coordinator_cpu_ceiling"] = ceiling
    assert row["cpu_quotas"][0].startswith("max ") and row["boot"]["--total-memory"] == str(2 << 30)
    assert set(row["archive"]) == {"checkpoint.img", "pages_meta.img", "pages.img"}
    assert all(v["bytes"] > 0 and re.fullmatch("[0-9a-f]{64}", v["sha256"]) for v in row["archive"].values())
    record["status"] = "PASS"
    record["verified_ns"] = time.time_ns()
    record["execution_to_verdict_ns"] = record["verified_ns"] - record["execution_finished_ns"]
    save()

def main():
    for arm in SOURCES:
        select(arm)
        assert run("build-" + arm, BAZEL + ["build", *OPTIONS, TARGET]) == 0
    for arm in SOURCES:
        observe(arm, -1)
    for number in range(12):
        for arm in PERMUTATIONS[number % 6]:
            observe(arm, number)
    actual = [(r["round"], r["arm"]) for r in RECEIPT["rows"] if r["round"] >= 0]
    expected = [(number, arm) for number in range(12) for arm in PERMUTATIONS[number % 6]]
    assert actual == expected and all(r["status"] == "PASS" for r in RECEIPT["rows"])
    cpus = {(r["observations"][0]["coordinator_cpus"], r["observations"][0]["sentry_allowed_cpus"], tuple(r["observations"][0]["cpu_quotas"]), tuple(sorted(r["observations"][0]["boot"].items()))) for r in RECEIPT["rows"]}
    assert len(cpus) == 1, cpus
    for arm in SOURCES:
        lines = []
        for record in RECEIPT["rows"]:
            if record["arm"] != arm or record["round"] < 0:
                continue
            row = record["observations"][0]
            for metric in ["checkpoint_ns", "checkpoint_exit_ns"]:
                lines.append(f"BenchmarkSave/{metric} 1 {row[metric]} ns/op")
            for metric in ["before_rss", "before_hwm", "lifetime_maxrss", "observed_save_exit_rss"]:
                lines.append(f"BenchmarkSave/{metric} 1 {row[metric]} B/op")
        (OUT / (arm + ".bench")).write_text("\n".join(lines) + "\n")
    RECEIPT["status"] = "PASS"
    RECEIPT["verified_ns"] = time.time_ns()
    save()

if __name__ == "__main__":
    try:
        save()
        main()
    except Exception as err:
        RECEIPT["error_type"] = type(err).__name__
        RECEIPT["status"] = "FAIL" if any(r["status"] == "FAIL" for r in RECEIPT["rows"]) or any(c["exit"] for c in RECEIPT["commands"]) else "INCOMPLETE"
        save()
        raise
