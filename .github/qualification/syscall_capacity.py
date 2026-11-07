"""Measure kernel capacity with two probes matching stock syscall TestRunners."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlparse
import uuid


WORK = Path(os.environ["RUNNER_TEMP"]) / "syscall-default-capacity"
RESULT = WORK / "receipt.json"
BASE = "f6d6788e84731673a155c9fff6f28b8624e34c50"
BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx6g"]
MEMORY = "//test/syscalls:memory_accounting_test_runsc_systrap_directfs"
ORIGINAL = {"//test/syscalls:processes_test_runsc_ptrace": "medium", MEMORY + "_save": "large",
            MEMORY + "_save_resume": "large"}
PROBES = {"//test/rbe/syscall_capacity:medium": "medium", "//test/rbe/syscall_capacity:large": "large"}
COMMON = ["--config=x86_64", "--host_platform=//tools/bazeldefs:linux_amd64",
          "--inject_repository=syscall_size_worker=" + str(WORK / "worker"),
          "--extra_execution_platforms=@syscall_size_worker//:amd64", "--jobs=16", "--remote_download_outputs=minimal",
          "--remote_download_regex=.*testlogs/.*/test[.](xml|log)$", "--spawn_strategy=remote",
          "--strategy=TestRunner=remote", "--noremote_local_fallback", "--modify_execution_info=^TestRunner$=+no-remote-cache",
          "--test_tag_filters=", "--build_tests_only", "--flaky_test_attempts=1", "--nocache_test_results",
          "--remote_timeout=900", "--test_timeout=900", "--keep_going"]


def save(doc):
    data = json.dumps(doc, indent=2) + "\n"
    assert len(data) < 100000 and not re.search(r"(?i)authorization\s*[:=]|x-buildbuddy-api-key|github_pat_|gh[pousr]_", data)
    RESULT.write_text(data)


def compare(doc):
    sys.path.insert(0, str(WORK))
    from graph import preflight

    def checkpoint(graph):
        doc["graph"] = {k: v for k, v in graph.items() if k not in ("diagnostics", "compiler_diagnostics")}
        save(doc)

    def profile(row):
        return row["size"], row["worker"], row["platform"]

    analysis = preflight(WORK, BAZEL, COMMON, ORIGINAL | PROBES, checkpoint)
    checkpoint(analysis)
    if not analysis["qualified"]:
        return False
    by_size = {size: profile(analysis["runners"][probe]) for probe, size in PROBES.items()}
    doc["matched"] = all(profile(analysis["runners"][target]) == by_size[size] for target, size in ORIGINAL.items())
    return doc["matched"]


def observe(row, label, outputs):
    f, = [f for f in outputs if f["name"] == "test.log"]
    path = Path("bazel-testlogs/test/rbe/syscall_capacity") / PROBES[label] / "test.log"
    assert 0 < path.stat().st_size <= 512000
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    uri = urlparse(f.get("uri", ""))
    ref = re.search(r"/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/(\d+)$", uri.path)
    if ref:
        bound = sha == ref[1] and len(data) == int(ref[2])
    elif f.get("digest"):
        bound = sha == f["digest"] and len(data) == int(f["length"])
    else:
        bound = uri.scheme == "file" and not uri.netloc and Path(unquote(uri.path)).resolve() == path.resolve()
    pattern = (rb"^SYSCALL_CAPACITY size=(medium|large) total_kib=(\d{1,12}) available_kib=(\d{1,12}) "
               rb"swap_kib=(\d{1,12}) threads_max=(\d{1,10}) cpu=x86_64 uid=0$")
    (size, total, avail, swap, threads), = re.findall(pattern, data, re.M)
    row.update(size=size.decode(), total_kib=int(total), available_kib=int(avail), swap_kib=int(swap),
               threads_max=int(threads), cpu="x86_64", uid=0, original_log=bound, sha256=sha)
    row["verified"] = (bound and row["status"] == "PASSED" and row["attempt"] == [1, 1, 1]
                       and row["strategy"] == "remote" and not row["cached_local"] and not row["cached_remote"]
                       and row["size"] == PROBES[label])


def measure(doc):
    assert doc["matched"]
    inv = str(uuid.uuid4())
    doc.update(invocation=inv, probes={})
    save(doc)
    raw = WORK / "events.json"
    with (WORK / "physical.log").open("wb") as log:
        doc["exit"] = subprocess.run([*BAZEL, "--bazelrc=" + str(WORK / "remote.bazelrc"), "test", *COMMON,
                                      "--build_event_json_file=" + str(raw), "--invocation_id=" + inv, *PROBES],
                                     stdout=log, stderr=log, timeout=700).returncode
    started, finished = [], []
    for line in raw.open():
        event = json.loads(line)
        if "started" in event:
            started.append(event["started"].get("uuid"))
        if "finished" in event:
            finished.append(event["finished"].get("exitCode", {}).get("code", 0))
        if "testResult" not in event:
            continue
        ident, res = event["id"]["testResult"], event["testResult"]
        label = ident["label"].lstrip("@")
        assert label in PROBES and label not in doc["probes"]
        info = res.get("executionInfo", {})
        row = doc["probes"][label] = {"verified": False, "status": res.get("status"), "strategy": info.get("strategy"),
                                      "cached_local": bool(res.get("cachedLocally")), "cached_remote": bool(info.get("cachedRemotely")),
                                      "attempt": [ident.get(k, 1) for k in ("run", "shard", "attempt")]}
        save(doc)
        try:
            observe(row, label, res["testActionOutput"])
        except (KeyError, OSError, AssertionError, ValueError) as exc:
            row["collection_error"] = type(exc).__name__
        save(doc)
    doc.update(started=started, finished=finished)
    return (doc["exit"] == 0 and started == [inv] and finished == [0] and doc["probes"].keys() == PROBES.keys()
            and all(row["verified"] for row in doc["probes"].values()))


def main():
    assert subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip() == BASE
    phase, = sys.argv[1:]
    doc = json.loads(RESULT.read_text()) if RESULT.is_file() else {"product_parent": BASE, "physical_roots": list(PROBES)}
    try:
        ok = {"graph": compare, "observe": measure}[phase](doc)
        doc[phase + "_verified"] = ok
        return ok
    finally:
        save(doc)


if __name__ == "__main__":
    raise SystemExit(not main())
