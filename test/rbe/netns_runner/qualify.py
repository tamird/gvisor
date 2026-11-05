"""Classify a namespace launch separately from the seven product tests."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import zipfile

import full

TARGET = "//runsc/sandbox:sandbox_test"
PRODUCT = full.PRODUCT
CASES = {
    "TestCollectLinksAndRoutes_" + case
    for case in ("DownInterface", "IPv6Disabled", "LoopbackExtraRoutes", "LoopbackOnly",
                 "MultipleInterfaces", "NoUsableAddresses", "SingleInterface")
}


def collect(arch, where, row):
    q = full.q
    assert q.git("rev-parse", "HEAD^^") == PRODUCT
    assert q.git("rev-parse", "HEAD:runsc/sandbox") == q.git("rev-parse", PRODUCT + ":runsc/sandbox")
    with zipfile.ZipFile(q.TMP / "original.zip") as archive:
        prior = json.loads(archive.read(arch + ".json"))
    source = prior["targets"][TARGET]["results"][0]["xml"]
    assert set(source["failure_categories"]) == CASES
    assert all("operation not permitted" in v for v in source["failure_categories"].values())
    row.update(source=PRODUCT, baseline_xml=source["sha256"], baseline_invocation=prior["events"]["invocation"])
    work = q.TMP / (arch + "-" + where)
    work.mkdir()
    invocation = str(uuid.uuid4())
    platform = "//test/rbe/release_prewarmer:" + arch
    flags = [
        "--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch],
        "--extra_execution_platforms=" + platform, "--host_platform=" + platform,
        "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
        "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
        "--extra_toolchains=//test/rbe/protoc_check:static_python" + ("_arm64" if arch == "arm64" else ""),
        "--noincompatible_exclude_starlark_flags_from_exec_config", "--lockfile_mode=off",
        "--incompatible_use_native_posix_test_wrapper", "--nocache_test_results",
        "--test_env=GO_TEST_WRAP_TESTV=1", "--runs_per_test=1", "--flaky_test_attempts=1",
        "--run_under=//test/rbe/netns_runner", "--test_filter=^TestCollectLinksAndRoutes_",
        "--jobs=32", "--color=no", "--curses=no", "--remote_download_outputs=toplevel",
        "--remote_download_regex=^bazel-out/[^/]+/(testlogs/runsc/sandbox/sandbox_test/test[.](xml|log)|bin/external/bazel_tools/tools/test/compiled_posix_test_wrapper)$",
        "--nobuild_event_json_file_path_conversion", "--build_event_publish_all_actions",
        "--invocation_id=" + invocation, "--build_event_json_file=" + str(work / "events"),
        "--execution_log_compact_file=" + str(work / "spawns"),
    ]
    if where == "coordinator":
        flags.append("--strategy=TestRunner=local")
    row["invocation"] = invocation
    with (work / "log").open("wb") as out:
        proc = subprocess.run(["timeout", "--signal=INT", "--kill-after=15", "660", full.BAZEL, *q.PROOF,
                               "test", *flags, TARGET], stdout=out, stderr=subprocess.STDOUT)
    row.update(bazel_exit=proc.returncode, diagnostics=q.diagnostics(work / "log"))
    q.save(arch + "-" + where, row)
    targets = {TARGET: {key: [] for key in ("results", "summaries", "completed", "aborted", "actions", "xml_generators")}}
    events = full.events(work / "events", targets, {})
    row["event_exit"] = events.get("exit")
    assert events["invocation"] == invocation
    results = targets[TARGET]["results"]
    row["test_results"] = len(results)
    if not results:
        return
    result, = results
    row.update(result_status=result["status"], cached=result["cached_local"] or result["cached_remote"],
               cases=result.get("xml", {}).get("named", {}))
    for line in (work / "events").open():
        event = json.loads(line)
        if "testResult" in event and event["id"]["testResult"]["label"] == TARGET:
            log, = (f for f in event["testResult"]["testActionOutput"] if f["name"] == "test.log")
            _, raw = full.test_output(log, TARGET, {}, "test.log", 2 * 1024 * 1024)
            row["markers"] = re.findall(r"^ROUTE_PROBE (?:[a-z-]+) (?:errno=[0-9]+|changed=(?:true|false)|started|error)$",
                                        raw.decode(errors="replace"), re.M)
            row["missing_shell"] = b"/bin/bash" in raw and b"No such file" in raw
            row["log_sha256"] = hashlib.sha256(raw).hexdigest()
    wrapper = {"stock": full.stock_wrapper(arch)}
    row["spawns"] = full.actions(work / "spawns", arch, targets, wrapper)["runners"]
    actions = targets[TARGET]["actions"]
    row["runners"] = [[a["runner"], a["cache_hit"], a["matched_platform"], a["exit"]] for a in actions]
    assert len(actions) == 1 and not row["cached"] and not actions[0]["cache_hit"]
    if where == "worker":
        assert actions[0]["matched_platform"] and actions[0]["runner"] == "remote"
    else:
        assert actions[0]["runner"] in ("local", "standalone")
    markers = row.get("markers", [])
    if result["status"] == "PASSED" and proc.returncode == 0:
        assert set(row["cases"]["passed"]) == CASES and not row["cases"]["skipped"] and not row["cases"]["failed"]
        assert "ROUTE_PROBE private-test started" in markers
        row["outcome"] = "SEVEN_PASSED"
    elif set(row["cases"].get("passed", [])) & CASES == set() and (
        "ROUTE_PROBE private-test-start errno=1" in markers or "ROUTE_PROBE private-test-start errno=13" in markers
    ):
        assert "ROUTE_PROBE private-test started" not in markers
        row["outcome"] = "CHILD_START_DENIED"


if __name__ == "__main__":
    arch, where = sys.argv[1:]
    row = {"arch": arch, "where": where, "outcome": "INCOMPLETE"}
    try:
        collect(arch, where, row)
    except (ValueError, KeyError, OSError, AssertionError, subprocess.SubprocessError) as err:
        row["collection_error"] = full.fault(err)
    finally:
        full.q.save(arch + "-" + where, row)
    sys.exit(row["outcome"] == "INCOMPLETE")
