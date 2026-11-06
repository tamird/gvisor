"""Run the same two native packet-ring cases against stock and corrected receivers."""
import hashlib
import json
import re
import subprocess
import sys
import uuid

import full

TARGET = "//pkg/tcpip/link/fdbased:fdbased_test"
SOURCES = {"stock": "1f49f256eae8d2dd2c69ea4cfe9ef2a5bb05516d",
           "fixed": "8311cfcaedcaf8dfbd221e0a09ef4ff4fd36af37"}
FILES = ["pkg/tcpip/link/fdbased/" + name for name in
         ("endpoint.go", "mmap.go", "mmap_unsafe.go", "packet_dispatchers.go", "processors.go")]


def collect(arch, variant, row):
    q = full.q
    assert arch in ("amd64", "arm64") and variant in SOURCES
    source = SOURCES[variant]
    subprocess.run(["git", "diff", "--quiet", source, "--", *FILES], check=True)
    names = {"TestPacketMMapBeforeAttach", "TestPacketMMapDeliversUDP"}
    row.update(source=source, expected=sorted(names),
               fixture=q.git("rev-parse", "HEAD:pkg/tcpip/link/fdbased/packet_ring_linux_test.go"))
    work = q.TMP / (arch + "-" + variant)
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
        "--run_under=//test/rbe/netns_runner", "--test_filter=^(" + "|".join(sorted(names)) + ")$",
        "--jobs=32", "--color=no", "--curses=no", "--remote_download_outputs=toplevel",
        "--remote_download_regex=^bazel-out/[^/]+/(testlogs/pkg/tcpip/link/fdbased/fdbased_test/test[.](xml|log)|bin/external/bazel_tools/tools/test/compiled_posix_test_wrapper)$",
        "--nobuild_event_json_file_path_conversion", "--build_event_publish_all_actions",
        "--invocation_id=" + invocation, "--build_event_json_file=" + str(work / "events"),
        "--execution_log_compact_file=" + str(work / "spawns"),
    ]
    row["invocation"] = invocation
    with (work / "log").open("wb") as out:
        proc = subprocess.run(["timeout", "--signal=INT", "--kill-after=15", "660", full.BAZEL, *q.PROOF,
                               "test", *flags, TARGET], stdout=out, stderr=subprocess.STDOUT)
    row.update(bazel_exit=proc.returncode, diagnostics=q.diagnostics(work / "log"))
    q.save(arch + "-" + variant, row)
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
            log_text = raw.decode(errors="replace")
            row["rings"] = re.findall(r"PACKET_RING setup arch=(amd64|arm64) page=([0-9]+) bytes=([0-9]+)", log_text)
            row["udp_delivered"] = "PACKET_RING delivered private loopback UDP" in log_text
            row["surviving_worker"] = "PACKET_RING worker survived Close and Wait" in log_text
            row["errors"] = [s for s in ("operation not permitted", "permission denied", "invalid argument",
                                         "network is unreachable", "timed out waiting", "no such device",
                                         "failed to enable PACKET_RX_RING", "unix.Mmap", "private loopback namespace")
                             if s in log_text]
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
    assert not row["spawns"].get("wrong_execution", 0)
    assert len(row["rings"]) == 2 and row["rings"][0] == row["rings"][1]
    worker, page, size = row["rings"][0]
    assert worker == arch and int(size) == 2101248 and int(page) > 0
    assert row["udp_delivered"] and not row["cases"]["skipped"] and not x["unnamed"]
    if variant == "stock":
        assert row["surviving_worker"] and result["status"] == "FAILED"
        assert proc.returncode == events["exit"] == 3 and action["exit"] != 0
        assert row["cases"]["failed"] == ["TestPacketMMapBeforeAttach"]
        assert row["cases"]["passed"] == ["TestPacketMMapDeliversUDP"]
    else:
        assert not row["surviving_worker"] and result["status"] == "PASSED"
        assert action["exit"] == proc.returncode == events["exit"] == 0
        assert set(row["cases"]["passed"]) == names and not row["cases"]["failed"]
    row["outcome"] = "VERIFIED"


if __name__ == "__main__":
    arch, variant = sys.argv[1:]
    row = {"arch": arch, "variant": variant, "target": TARGET, "outcome": "INCOMPLETE"}
    try:
        collect(arch, variant, row)
    except (ValueError, KeyError, OSError, AssertionError, subprocess.SubprocessError) as err:
        row["collection_error"] = full.fault(err)
    finally:
        full.q.save(arch + "-" + variant, row)
    sys.exit(row["outcome"] != "VERIFIED")
