"""Qualify the authored default-route tests on the two native workers."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid
import zipfile

import full

TARGET = "//runsc/sandbox:sandbox_test"
PRODUCT = "9d0caabe320ab95fa3c7e5e84c5dd648287e901c"
ORIGINAL = {
    "TestCollectLinksAndRoutes_" + case
    for case in ("DownInterface", "IPv6Disabled", "LoopbackExtraRoutes", "LoopbackOnly",
                 "MultipleInterfaces", "NoUsableAddresses", "SingleInterface")
}


CONTROLS = {
    "TestPrimaryInterface", "TestLoopbackLinkDefaultRoute", "TestPrimaryInterfacePerLinkDefaults",
    "TestRouteCollectionPreservesDefaults", "TestCollectedDefaultsAcrossInterfaces",
    "TestPrimaryInterfaceAddressFamily", "TestCollectedDefaultWithoutFamilyAddress",
}


def expected_names():
    names = ORIGINAL | CONTROLS
    text = Path("runsc/sandbox/network_test.go").read_text()
    for group, count in (("TestPrimaryInterface", 12), ("TestPrimaryInterfacePerLinkDefaults", 11)):
        body = text.split("func " + group + "(", 1)[1].split("\nfunc ", 1)[0]
        cases = re.findall(r'name:\s*"([^"]+)"', body)
        assert len(cases) == count
        names.update(group + "/" + name.replace(" ", "_") for name in cases)
    for family in ("IPv4", "IPv6"):
        names.add("TestPrimaryInterfaceAddressFamily/" + family)
        for kind in ("gateway-first", "on-link-first", "two-gateways"):
            names.add(f"TestCollectedDefaultsAcrossInterfaces/{family}/{kind}")
        for kind in ("gateway", "on-link", "mixed", "two-gateways"):
            case = f"TestRouteCollectionPreservesDefaults/{family}/{kind}"
            names.add(case)
            if family == "IPv6":
                names.add(case + "/disabled")
    assert len(names) == 57
    return names


def project_log(raw, names):
    sites = {}
    active = None
    for line in raw.decode(errors="replace").splitlines():
        if match := re.search(r"(?:=== RUN\s+|--- (?:FAIL|PASS|SKIP): )([^\s]+)", line):
            active = match[1] if match[1] in names else None
        if active and (site := re.search(r"(?:^|/)network_test[.]go:(\d+):|\bnetwork_test[.]go:(\d+):", line)):
            tokens = [term for term in ("mismatch", "default", "gateway", "primaryInterface", "address",
                      "netstack selected", "unexpected", "operation not permitted", "permission denied",
                      "invalid argument", "file exists", "not supported", "network is unreachable",
                      "cannot assign requested address", "no such file") if term in line]
            rows = sites.setdefault(active, [])
            if len(rows) < 8:
                rows.append({"line": int(site[1] or site[2]), "terms": tokens})
    return sites


def collect(arch, row):
    q = full.q
    assert arch in ("amd64", "arm64") and q.git("rev-parse", "HEAD^") == PRODUCT
    names = expected_names()
    with zipfile.ZipFile(q.TMP / "prior-route.zip") as archive:
        prior = json.loads(archive.read(arch + "-worker.json"))
    different = {"TestCollectLinksAndRoutes_" + name for name in ("SingleInterface", "MultipleInterfaces", "IPv6Disabled")}
    assert prior["source"] == full.PRODUCT and set(prior["cases"]["failed"]) == different
    assert set(prior["cases"]["passed"]) == ORIGINAL - different and not prior["cases"]["skipped"]
    assert prior["runners"] == [["remote", False, True, 1]] and not prior["cached"]
    row.update(source=PRODUCT, upstream="1be6183240b96e6ac4ac51a9f03463ae3077a43f",
               prior_invocation=prior["invocation"], prior_log_sha256=prior["log_sha256"],
               prior_failed=sorted(different), expected=len(names))
    work = q.TMP / (arch + "-worker")
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
        "--run_under=//test/rbe/netns_runner", "--test_filter=^(" + "|".join(sorted(ORIGINAL | CONTROLS)) + ")$",
        "--jobs=32", "--color=no", "--curses=no", "--remote_download_outputs=toplevel",
        "--remote_download_regex=^bazel-out/[^/]+/(testlogs/runsc/sandbox/sandbox_test/test[.](xml|log)|bin/external/bazel_tools/tools/test/compiled_posix_test_wrapper)$",
        "--nobuild_event_json_file_path_conversion", "--build_event_publish_all_actions",
        "--invocation_id=" + invocation, "--build_event_json_file=" + str(work / "events"),
        "--execution_log_compact_file=" + str(work / "spawns"),
    ]
    row["invocation"] = invocation
    with (work / "log").open("wb") as out:
        proc = subprocess.run(["timeout", "--signal=INT", "--kill-after=15", "660", full.BAZEL, *q.PROOF,
                               "test", *flags, TARGET], stdout=out, stderr=subprocess.STDOUT)
    row.update(bazel_exit=proc.returncode, diagnostics=q.diagnostics(work / "log"))
    q.save(arch + "-worker", row)
    targets = {TARGET: {key: [] for key in ("results", "summaries", "completed", "aborted", "actions", "xml_generators")}}
    configs = {}
    events = full.events(work / "events", targets, configs)
    row["failures"] = events["failures"]
    row["event_exit"] = events.get("exit")
    assert events["invocation"] == invocation
    results = targets[TARGET]["results"]
    row["test_results"] = len(results)
    if not results:
        return
    result, = results
    row.update(result_status=result["status"], cached=result["cached_local"] or result["cached_remote"],
               cases=result.get("xml", {}).get("named", {}))
    x = result.get("xml", {})
    # Keep actual original result/leaf evidence even if a later projection fails.
    row.update(xml={k: x[k] for k in ("sha256", "bytes", "counts", "error") if k in x},
               test_id=[result[k] for k in ("run", "shard", "attempt")],
               target_cpu=configs.get(result["config"]), missing=sorted(names - {
                   n for group in row["cases"].values() for n in group}))
    for line in (work / "events").open():
        event = json.loads(line)
        if "testResult" in event and event["id"]["testResult"]["label"] == TARGET:
            log, = (f for f in event["testResult"]["testActionOutput"] if f["name"] == "test.log")
            _, raw = full.test_output(log, TARGET, {}, "test.log", 2 * 1024 * 1024)
            row["markers"] = re.findall(r"^ROUTE_PROBE (?:[a-z-]+) (?:errno=[0-9]+|changed=(?:true|false)|started|error)$",
                                        raw.decode(errors="replace"), re.M)
            row["diagnostic_sites"] = project_log(raw, names)
            row["log_sha256"] = hashlib.sha256(raw).hexdigest()
    wrapper = {"stock": full.stock_wrapper(arch)}
    row["spawns"] = full.actions(work / "spawns", arch, targets, wrapper)["runners"]
    actions = targets[TARGET]["actions"]
    row["runners"] = [[a["runner"], a["cache_hit"], a["matched_platform"], a["exit"]] for a in actions]
    assert len(actions) == 1 and not row["cached"] and not actions[0]["cache_hit"]
    action, = actions
    row["action"] = {k: action[k] for k in ("digest", "exit", "wrapper_path", "native_wrapper", "matched_platform")}
    assert action["native_wrapper"] and action["matched_platform"] and action["runner"] == "remote"
    summary, = targets[TARGET]["summaries"]
    completion, = targets[TARGET]["completed"]
    assert completion["config"] == summary["config"] == result["config"] and completion["built"]
    assert row["target_cpu"] == {"amd64": "k8", "arm64": "aarch64"}[arch]
    assert row["test_id"] == [1, 1, 1] and summary["status"] == result["status"] and not events.get("unexpected_test")
    assert "ROUTE_PROBE private-test started" in row["markers"]
    if result["status"] == "FAILED" and proc.returncode == events["exit"] == 3 and action["exit"] != 0:
        row["outcome"] = "TEST_FAILED"
        return
    assert result["status"] == "PASSED" and action["exit"] == proc.returncode == events["exit"] == 0
    assert x["counts"] == {"passed": len(names), "failed": 0, "skipped": 0}
    assert set(row["cases"]["passed"]) == names and not row["cases"]["failed"] and not row["cases"]["skipped"]
    row["outcome"] = "PASSED"


if __name__ == "__main__":
    arch, = sys.argv[1:]
    row = {"arch": arch, "target": TARGET, "outcome": "INCOMPLETE"}
    try:
        collect(arch, row)
    except (ValueError, KeyError, OSError, AssertionError, subprocess.SubprocessError) as err:
        row["collection_error"] = full.fault(err)
    finally:
        full.q.save(arch + "-worker", row)
    sys.exit(row["outcome"] != "PASSED")
