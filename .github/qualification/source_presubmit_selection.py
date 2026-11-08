"""Bind exact roots to the verified mixed graph and submit the rendered Buildkite step."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid

HISTORICAL_SOURCE = "f6d6788e84731673a155c9fff6f28b8624e34c50"
PINRING_SOURCE = "5c8ad1d07625045ba8bb2a21e2cae100557b569a"
SOURCE = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
CONTROL = "//test/syscalls:uname_test_native"
TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "llvm-source-selection.json"
RESULTS = TMP / "source-presubmit-results"
LABEL = re.compile(r"//[A-Za-z0-9_.+/-]*:[A-Za-z0-9_.+/-]+")
# Member receipt.json from tamird/gvisor Actions artifact 13572296525.
HISTORICAL_RECEIPT_SHA256 = "5b82f00dc28b768fd4129813f6e82f852c0a204b1b04d0051b7eec975edb0fbd"
SUFFIX = "_presubmit_arm64"
FIRECRACKER = {
    "//examples/sandboxexec/go/docprocessor:docprocessor_test", "//runsc/cmd:cmd_test",
    "//runsc/sandbox:sandbox_test", "//sandboxexec/sandbox/python:sandbox_py_test",
    "//sandboxexec/sandbox:sandbox_test", CONTROL,
}
if SOURCE == PINRING_SOURCE:
    FIRECRACKER.add("//pkg/pinring:pinring_test")
PROBE = '''set -eu
machine=$(/bin/uname -m)
case "$TEST_TARGET" in
    *'_presubmit_arm64') test "$machine" = aarch64; field='CPU implementer' ;;
    *) test "$machine" = x86_64; field='vendor_id' ;;
esac
/bin/grep -Eq "^$field[[:space:]]*:" /proc/cpuinfo
printf 'MIXED_NATIVE %s %s %s %s %s\\n' "$TEST_TARGET" "$machine" "$(/usr/bin/id -u)" "${TEST_SHARD_INDEX:-0}" "${TEST_TOTAL_SHARDS:-1}"
exec "$@"'''
RUN_UNDER = "--run_under=" + shlex.join(["/bin/sh", "-c", PROBE, "native-probe"])
stage = {"source": SOURCE, "phases": {}}


def save():
    OUT.write_text(json.dumps(stage, sort_keys=True, separators=(",", ":")))


def canonical_step():
    steps = json.loads((TMP / "pipeline.json").read_text())["steps"]
    step, = [s for s in steps if isinstance(s, dict) and s.get("key") == "rbe-source"]
    assert step["command"] == '"${BUILDKITE_SOURCE_BAZEL}" test --config=source-presubmit-mixed'
    assert step["if"] == 'build.env("STAGED_BINARIES") == null'
    assert step["agents"] == {"arch": "amd64", "cgroup": "v2", "queue": "default"}
    assert not os.environ.get("STAGED_BINARIES")
    return step


def select(name, opts, tests_only):
    # Use the same source-controlled pattern file as the remote submission.
    bep = TMP / ("selection-"+name+".bep")
    log = TMP / ("selection-"+name+".log")
    args = ["bazelisk", "test", *opts, "--jobs=4", "--nobuild", "--build_event_json_file="+str(bep)]
    if tests_only: args.append("--build_tests_only")
    with log.open("w") as f:
        code = subprocess.run(args, stdout=f, stderr=subprocess.STDOUT).returncode
    stage["phases"][name] = {"exit":code}
    save()
    rows = {}
    suites = set()
    finish = []
    digest = hashlib.sha256()
    with bep.open("rb") as events:
        for line in events:
            digest.update(line)
            e = json.loads(line)
            ident = e["id"]
            assert "testResult" not in e and "testSummary" not in e and "action" not in e
            if e.get("configured", {}).get("targetKind") == "test_suite rule":
                suites.add(ident["targetConfigured"]["label"])
            if "aborted" in e:
                reason = e["aborted"]["reason"]
                assert reason in ("NO_BUILD", "SKIPPED"), reason
                target = ident.get("targetCompleted")
                if reason == "NO_BUILD" and target and not target.get("aspect"):
                    label = target["label"]
                    config = target["configuration"]["id"]
                    assert LABEL.fullmatch(label) and re.fullmatch(r"[0-9a-f]{64}", config)
                    assert label not in rows or rows[label] == config
                    rows[label] = config
            if "finished" in e:
                finish.append(e["finished"].get("exitCode"))
    assert code == 1 and finish == [{"code": 1, "name": "BUILD_FAILURE"}] and CONTROL in rows
    for suite in suites:
        rows.pop(suite, None)
    stage["suites"] = sorted(set(stage.get("suites", ())) | suites)
    stage["phases"][name].update(count=len(rows),bep_sha256=digest.hexdigest())
    stage[name]=rows
    save()
    return rows


def historical_selection(roots, tests):
    path = TMP / "13572296525-receipt.json"
    if path.is_file():
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == HISTORICAL_RECEIPT_SHA256
        return json.loads(data), "original_receipt"

    # The original coordinator passed no receipt. Reuse these original labels
    # only when both independently selected owner sets match exactly.
    def owner_digest(labels):
        return hashlib.sha256(("\n".join(sorted(labels)) + "\n").encode()).hexdigest()

    if (owner_digest(roots.keys() - tests.keys()) != "9e31ff09dacc4269ef991f6ef7267445134a77f0bc0cd196539f9f2fd1cd1ecd"
            or owner_digest(tests) != "121d63098403b87a32a48205a8d6150a493b2ad6e05df418cb26172197009b65"):
        raise ValueError(f"Owner inventory changed; supply artifact 13572296525 member receipt.json at {path}")
    multiple = {"//pkg/tcpip/stack:stack_x_test": 8, "//pkg/tcpip/transport/tcp/test/e2e:tcp_test": 4}
    shards = {label: multiple.get(label.removesuffix(SUFFIX), 1) for label in tests}
    return {"source": HISTORICAL_SOURCE, "target_results": roots, "expected_per_owner_shards": shards}, "original_owner_digests"


def analyze():
    step = canonical_step()
    patterns = Path("test/rbe/source-presubmit.targets").read_text().splitlines()
    stage.update(command=step["command"], public_patterns=[p for p in patterns if p and not p.startswith("#")])
    opts = ["--config=source-presubmit-mixed", "--spawn_strategy=local", "--strategy=TestRunner=local",
            "--remote_executor=", "--remote_cache=", "--bes_backend=", "--modify_execution_info=^TestRunner$=+no-remote-cache",
            "--test_env=GO_TEST_WRAP_TESTV=1", "--nocache_test_results", "--flaky_test_attempts=1", RUN_UNDER]
    roots = select("all", opts, False)
    tests = select("tests", opts, True)
    assert tests.keys() <= roots.keys()
    assert tests and roots and CONTROL + SUFFIX in tests
    workers = {label: "arm64" if label.endswith(SUFFIX) else "amd64" for label in tests}
    old, provenance = historical_selection(roots, tests)
    old_tests = old["expected_per_owner_shards"]
    old_builds = old["target_results"].keys() - old_tests.keys()
    builds = roots.keys() - tests.keys()
    drift = {}
    for name, previous, current in (("build_only", old_builds, builds), ("tests", old_tests.keys(), tests.keys())):
        drift[name] = {"missing": sorted(previous - current), "added": sorted(current - previous)}
    stage.update(test_workers=workers, firecracker_owners=sorted(label for label in tests if label.removesuffix(SUFFIX) in FIRECRACKER),
                 historical_artifact=13572296525, historical_source=old["source"], historical_mode=provenance, historical_changes=drift,
                 historical_shards=old_tests, invocation=str(uuid.uuid4()), eligible=True)
    save()
    print(f"Selected {len(tests)} test owners and {len(builds)} build-only roots", flush=True)


def execute():
    data = OUT.read_bytes()
    plan = json.loads(data)
    assert plan["eligible"] and plan["source"] == SOURCE
    with (TMP / "submitted-selection.sha256").open("x") as marker:
        marker.write(hashlib.sha256(data).hexdigest())
    RESULTS.mkdir(exist_ok=True)
    wrapper = TMP / "full-mixed-bazel"
    argv = ["bazelisk", "--bazelrc=" + str(TMP / "source-presubmit.bazelrc")]
    wrapper.write_text("#!/bin/sh\nset -eu\n" + shlex.join(["exec", *argv]) + ' "$@" ' + shlex.quote(RUN_UNDER) + " --invocation_id=" + str(uuid.UUID(plan["invocation"])) + "\n")
    wrapper.chmod(0o700)
    cmd = ["timeout", "--signal=INT", "--kill-after=60s", "90m", "/bin/bash", "-eu", "-c", canonical_step()["command"]]
    edge = TMP / "source-presubmit.bep"
    progress = {"source": SOURCE, "invocation": None, "terminal_targets": 0, "results": {}}
    counts = Counter()
    offset = 0
    with (TMP / "source-presubmit.log").open("wb") as log:
        process = subprocess.Popen(cmd, env=dict(os.environ, BUILDKITE_SOURCE_BAZEL=str(wrapper)), stdout=log, stderr=log)
        while True:
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                pass
            if edge.is_file():
                with edge.open("rb") as events:
                    events.seek(offset)
                    for raw in events:
                        if not raw.endswith(b"\n"):
                            break
                        offset += len(raw)
                        try:
                            e = json.loads(raw)
                        except ValueError:
                            continue
                        if start := e.get("started"):
                            progress["invocation"] = str(uuid.UUID(start["uuid"]))
                        if e.get("id", {}).get("targetCompleted"):
                            progress["terminal_targets"] += 1
                        if t := e.get("testResult"):
                            status = t.get("status", "NO_STATUS")
                            counts[status if re.fullmatch("[A-Z_]{1,32}", status) else "UNKNOWN"] += 1
            progress["results"] = dict(counts)
            (RESULTS / "progress.json").write_text(json.dumps(progress) + "\n")
            print(json.dumps(progress), flush=True)
            if process.returncode is not None:
                (TMP / "source-presubmit-exit").write_text(str(process.returncode) + "\n")
                return


if __name__ == "__main__":
    assert SOURCE in (HISTORICAL_SOURCE, "55e7265abbd508e773fa259c1ddd6e3182d5296e", PINRING_SOURCE)
    {"analyze": analyze, "execute": execute}[sys.argv[1]]()
