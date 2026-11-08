"""Run the real GPU driver tests on the two established native empty workers."""
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

sys.path.insert(0, str(Path(os.environ["RUNNER_TEMP"]) / "source-build-test"))
import build_test as original

OWNER = "//tools/gpu/drivers:drivers_test"
SUFFIX = OWNER[2:].replace(":", "/")
TOP = {"TestDriverInstalls", "TestVersionNotSupported", "TestHostDriverRequiresChecksum",
       "TestListSupportedDrivers", "TestDownload", "TestValidateChecksum"}
LEAVES = {"TestDriverInstalls/" + leaf for leaf in (
    "already_installed", "absent", "replace", "inspection_failure", "unsuitable_host", "read_failure",
    "checksum_failure", "uninstall_failure", "install_failure")}
DIGEST = re.compile("[0-9a-f]{64}")
REPORT = {"product": "56be21cd4260332c866fb42d5852072cf6d37619", "source_base": original.SOURCE,
          "upstream_product": "f5d014239449fd5ce9a72af493c40d7c579e367b",
          "patch_id": "d03bef59d7318388972124eb4e1ce5ac8001f066", "owner": OWNER, "cells": {}, "verified": False}
original.report = REPORT


def location(error):
    frame = error.__traceback__
    while frame.tb_next:
        frame = frame.tb_next
    return {"type": type(error).__name__, "line": frame.tb_lineno}


def suffix(value):
    if not value:
        return None
    name = Path(unquote(urlsplit(value).path)).name
    if name in {"drivers_test", "drivers_test.exe", "fake_installer", "test.log", "test.xml", "test.outputs",
                "test.outputs_manifest", "test.cache_status", "compiled_posix_test_wrapper", "posix_tw", "--generate-xml"}:
        return name
    return {"other_sha256": original.sha(name.encode()), "extension": next(
        (ext for ext in (".xml", ".log", ".zip", ".exe") if name.endswith(ext)), None)}


def outputs(result, row):
    data = {}
    entries = result.get("testActionOutput", [])
    row["output_names"] = [{"name": suffix(f.get("name")), "uri_suffix": suffix(f.get("uri"))} for f in entries[:24]]
    for name in ("test.log", "test.xml"):
        item, = (f for f in entries if f["name"] == name)
        uri = urlsplit(item["uri"])
        assert uri.scheme == "file" and not uri.netloc
        path = Path(unquote(uri.path))
        assert path.as_posix().endswith("/testlogs/" + SUFFIX + "/" + name)
        assert 0 < path.stat().st_size <= 8 << 20
        blob = path.read_bytes()
        meta = row[name] = {"sha256": original.sha(blob), "bytes": len(blob)}
        assert not item.get("digest") or item["digest"] == meta["sha256"]
        assert "length" not in item or int(item["length"]) == len(blob)
        data[name] = blob
    root = ElementTree.fromstring(data["test.xml"])
    cases = list(root.iter("testcase"))
    xml = Counter(c.get("name") for c in cases)
    passed = Counter(re.findall(r"(?m)^\s*--- PASS: (\S+) \(", data["test.log"].decode(errors="replace")))
    row["xml_case_count"] = len(cases)
    row["go_pass_count"] = sum(passed.values())
    row["driver_cases"] = {n: {"go_pass": passed[n], "xml": xml[n]} for n in sorted(LEAVES)}
    row["top_level_passes"] = {n: passed[n] for n in sorted(TOP)}
    assert root.tag in ("testsuite", "testsuites") and cases
    assert not any(x.tag in ("failure", "error", "skipped") for x in root.iter())
    assert not any(c.get("status") in ("notrun", "suppressed") or c.get("result") in ("skipped", "suppressed") for c in cases)
    assert {n for n in xml if n and n.startswith("TestDriverInstalls/")} == LEAVES
    assert all(xml[n] == passed[n] == 1 for n in LEAVES) and all(passed[n] == 1 for n in TOP)



def observe_bep(directory, row):
    completed, results = [], []
    with (directory / "bep").open() as stream:
        for line in stream:
            if not line.endswith("\n"):
                break
            event = json.loads(line)
            key = event["id"]
            if "started" in event and re.fullmatch("[0-9a-f-]{36}", event["started"].get("uuid", "")):
                row["invocation"] = event["started"]["uuid"]
            if "targetCompleted" in key and "completed" in event:
                target = key["targetCompleted"]
                if original.owner(target["label"]) == OWNER and not target.get("aspect"):
                    completed.append((target["configuration"]["id"], bool(event["completed"].get("success"))))
            if "testResult" in event:
                test = key["testResult"]
                row["total_test_results"] += 1
                if original.owner(test["label"]) == OWNER:
                    results.append((test, event["testResult"]))
    row["owner_completions"], row["owner_test_results"] = len(completed), len(results)
    if len(completed) == 1:
        config, row["target_completed"] = completed[0]
        row["target_config"] = config if DIGEST.fullmatch(config) else None
    (test, result), = results
    config = test["configuration"]["id"]
    info = result.get("executionInfo", {})
    status = result.get("status")
    row.update(test_config=config if DIGEST.fullmatch(config) else None,
               status=status if status in ("PASSED", "FAILED", "TIMEOUT", "INCOMPLETE", "FLAKY", "REMOTE_FAILURE",
                                           "FAILED_TO_BUILD", "TOOL_HALTED_BEFORE_TESTING") else "OTHER",
               remote=info.get("strategy") == "remote", cached=bool(result.get("cachedLocally") or info.get("cachedRemotely")))
    outputs(result, row)
    assert all(int(test.get(k, 1)) == 1 for k in ("run", "attempt", "shard"))
    assert completed == [(config, True)] and DIGEST.fullmatch(config) and row["total_test_results"] == 1
    assert row["status"] == "PASSED" and not row["cached"] and row["remote"] and row.get("invocation")


def observe_worker(directory, arch, row):
    compact = {}
    original.spawns(directory / "compact", compact, arch)
    assert compact["projection_exit"] == compact["decode_exit"] == 0
    spawns = [s for s in compact["spawns"] if s["kind"] == "TestRunner"]
    row["test_spawns"] = [{"owner_matches": s["owner"] == OWNER, "wrapper": suffix(s["exe"]),
                           "program": suffix(s["test_program"]), "cpu": s["cpu"] if s["cpu"] in original.ARCH else None,
                           "image_matches": s["image"], "remote": s["runner"] == "remote", "cached": s["cache"],
                           "exit": s["exit"], "action_digest": s["digest"] if DIGEST.fullmatch(s["digest"] or "") else None}
                          for s in spawns]
    primary, = [s for s in spawns if Path(s["test_program"] or "").name == "drivers_test"]
    wrapper = "posix_tw" if arch == "amd64" else "compiled_posix_test_wrapper"
    assert all(s["owner"] == OWNER and (s is primary or s["test_program"] == "--generate-xml") for s in spawns)
    assert all(s["image"] and s["cpu"] == arch and s["runner"] == "remote" and s["exit"] == 0
               and Path(s["exe"]).name == wrapper for s in spawns)
    assert not primary["cache"] and DIGEST.fullmatch(primary["digest"])
    row["worker"] = {"cpu": primary["cpu"], "oci_image_sha256": original.ARCH[arch][1], "wrapper": wrapper,
                     "primary_action_digest": primary["digest"]}



def run(arch):
    row = REPORT["cells"][arch] = {"total_test_results": 0, "errors": {}, "verified": False}
    directory = original.WORK / arch
    directory.mkdir()
    flags = original.opts(arch, native=True, remote=True)
    flags[flags.index("--jobs=32")] = "--jobs=4"
    flags = [f for f in flags if not f.startswith("--remote_download_regex=")]
    flags.extend(["--remote_download_regex=.*/test[.](log|xml)$", "--flaky_test_attempts=1", "--runs_per_test=1",
                  "--test_sharding_strategy=disabled", "--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1",
                  "--nobuild_event_json_file_path_conversion", "--build_event_json_file=" + str(directory / "bep"),
                  "--execution_log_compact_file=" + str(directory / "compact")])
    args = [*original.startup(native=True, remote=True), "test", *flags, OWNER]
    original.save()
    try:
        with (directory / "console").open("wb") as log:
            row["exit"] = subprocess.run(["timeout", "--signal=INT", "--kill-after=5s", "1195s", *args],
                                         stdout=log, stderr=subprocess.STDOUT).returncode
        console = (directory / "console").read_bytes()
        row["console_sha256"] = original.sha(console)
        row["signals"] = [s for s in ("No matching toolchains", "Error downloading", "No such file or directory",
                                     "FAILED_TO_FETCH", "No registered executors") if s.lower().encode() in console.lower()]
        for stage, observer, params in (("bep", observe_bep, (directory, row)), ("worker", observe_worker, (directory, arch, row))):
            try:
                observer(*params)
            except Exception as error:
                row["errors"][stage] = location(error)
            original.save()
        row["verified"] = row["exit"] == 0 and not row["errors"]
    except Exception as error:
        row["errors"]["run"] = location(error)
    original.save()


if __name__ == "__main__":
    for cpu in ("amd64", "arm64"):
        run(cpu)
    REPORT["verified"] = all(row["verified"] for row in REPORT["cells"].values())
    original.save()
    sys.exit(not REPORT["verified"])
