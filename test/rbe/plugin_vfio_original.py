"""Inspect original proposal actions and bytes without rebuilding or retesting."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import traceback
import zipfile

from cas_inputs import TMP, blob, cas, fetch, input_file, rpc

SOURCE = "92c5bed67c8cac88959b0a4cb64e0b52f92b2ec9"
ORIGINAL_SHA = "9d8dd3fd39c6fef6f917cca94c1555914df7731be4282838d1d31dcd04d77910"
TEST = "//external/tools/plugin-stack:backtrace_test"
OWNER = "@@+_repo_rules+libpluginstack//:libpluginstack"
INVOCATIONS = {"gnu": "36687a83-aee5-40db-8f7c-773a89dea0b0", "musl": "700caae9-326c-47b3-857f-c4e9201ba3e4"}
PROPS = {"Arch": "amd64", "OSFamily": "linux", "EstimatedMemory": "2GB",
         "container-image": "docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a",
         "workload-isolation-type": "firecracker"}
SYMBOLS = ["pci_vfio_read_config", "pci_vfio_map_resource", "rte_vfio_container_dma_map"]
PUBLIC = TMP / "vfio-original-evidence"


def now():
    return datetime.now(timezone.utc).isoformat()


def save(result):
    (PUBLIC / "result.json").write_text(json.dumps(result, indent=2) + "\n")


def same_digest(left, right):
    return left["hash"] == right["hash"] and int(left["sizeBytes"]) == int(right["sizeBytes"])


def error_info(error):
    return {"type": type(error).__name__, "line": traceback.extract_tb(error.__traceback__)[-1].lineno}


def action(row, item):
    digest = row["actionDigest"]
    summary = {"digest": digest, "mnemonic": row["actionMnemonic"], "target": row["targetLabel"],
               "stage": row.get("stage"), "rpc_code": row.get("status", {}).get("code", 0),
               "exit": row.get("exitCode", 0)}
    item.setdefault("actions", []).append(summary)
    if summary["rpc_code"] or summary["exit"]:
        item["status"] = "FAIL"
    assert row["stage"] in (4, "COMPLETED") and summary["rpc_code"] == summary["exit"] == 0
    response = row["executeResponse"]
    assert response.get("status", {}).get("code", 0) == response["result"].get("exitCode", 0) == 0
    summary["cached"] = response.get("cachedResult", False)
    result = response["result"]
    metadata = result.get("executionMetadata", row.get("executedActionMetadata", {}))
    summary["timing"] = {k: metadata[k] for k in ("executionStartTimestamp", "executionCompletedTimestamp") if k in metadata}
    original = cas(digest, "Action")
    command = cas(original["commandDigest"], "Command")
    properties = {p["name"]: p["value"] for p in command.get("platform", {}).get("properties", [])}
    summary["properties"] = {k: properties.get(k) for k in PROPS}
    summary["executable"] = Path(command["arguments"][0]).name
    assert summary["properties"] == PROPS
    return original, command, result


def output(command, result, basename):
    selected, = [f for f in result.get("outputFiles", []) if Path(f["path"]).name == basename]
    assert selected["path"] in command.get("outputPaths", []) + command.get("outputFiles", [])
    return selected


def recover(name, old, item):
    invocation = old["invocation"]
    rows = rpc("GetExecution", {"executionLookup": {"invocationId": invocation}, "inlineExecuteResponse": True})["execution"]
    assert len(rows) < 10000
    selected = {}
    for mnemonic, owner in (("CcMakeMakeRule", OWNER), ("GoLink", TEST), ("TestRunner", TEST)):
        matches = [r for r in rows if r.get("actionMnemonic") == mnemonic and r.get("targetLabel") == owner]
        item.setdefault("selection", {})[mnemonic] = [{"digest": r["actionDigest"], "stage": r.get("stage")} for r in matches]
        selected[mnemonic], = matches
    if name == "gnu":
        for retained in old["actions"]:
            assert same_digest(selected[retained["mnemonic"]]["actionDigest"], retained["digest"])
    make, make_command, make_result = action(selected["CcMakeMakeRule"], item)
    archive = output(make_command, make_result, "libpluginstack.a")
    archive_bytes = blob(archive["digest"], invocation)
    path = TMP / (name + ".a")
    path.write_bytes(archive_bytes)
    symbols = subprocess.check_output(["nm", "--defined-only", str(path)], stderr=subprocess.DEVNULL, text=True)
    item["archive"] = {"sha256": archive["digest"]["hash"], "bytes": len(archive_bytes),
                       "defined_symbols": [s for s in SYMBOLS if re.search(r"(?m)\b" + s + r"$", symbols)]}
    assert item["archive"]["defined_symbols"] == SYMBOLS
    if name == "gnu":
        assert item["archive"] == old["archive"]
    wrapper, = [a for a in make_command["arguments"] if a.endswith("/libpluginstack_foreign_cc/wrapper_build_script.sh")]
    script_path = wrapper.replace("/wrapper_build_script.sh", "/build_script.sh")
    script_file = input_file(make["inputRootDigest"], script_path)
    script = blob(script_file["digest"], invocation).decode()
    compiler = "clang" if name == "musl" else "gcc"
    item["make_script"] = {"digest": script_file["digest"], "compiler": compiler,
                           "musl_target": "x86_64-linux-musl" in script,
                           "largefile_aliases": "EXTRA_CPPFLAGS=-D_LARGEFILE64_SOURCE" in script,
                           "vfio_opt_out": "RTE_EAL_VFIO=n" in script}
    assert "x86_64-native-linuxapp-" + compiler in script
    assert item["make_script"]["musl_target"] == item["make_script"]["largefile_aliases"] == (name == "musl")
    assert not item["make_script"]["vfio_opt_out"]
    link, link_command, link_result = action(selected["GoLink"], item)
    binary = output(link_command, link_result, "backtrace_test")
    assert same_digest(input_file(link["inputRootDigest"], archive["path"])["digest"], archive["digest"])
    item["archive_link_input_verified"] = True
    data = blob(binary["digest"], invocation)
    path = TMP / (name + ".elf")
    path.write_bytes(data)
    headers = subprocess.check_output(["readelf", "-hld", str(path)], text=True)
    item["binary"] = {"sha256": binary["digest"]["hash"], "bytes": len(data), "path": binary["path"],
                      "amd64": "Advanced Micro Devices X86-64" in headers,
                      "interpreter": re.findall(r"Requesting program interpreter: (.*)]", headers),
                      "needed": re.findall(r"\(NEEDED\).*\[(.*?)\]", headers)}
    symbols = subprocess.run(["nm", "--defined-only", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    item["binary"]["symbol_inspection_exit"] = symbols.returncode
    item["binary"]["observed_vfio_symbols"] = [s for s in SYMBOLS if re.search(r"(?m)\b" + s + r"$", symbols.stdout)]
    item["cquery_output_was_actual_link_output"] = binary["path"] in old["selected"][TEST]
    assert item["binary"]["amd64"] and not item["binary"]["interpreter"] and not item["binary"]["needed"]
    test, test_command, test_result = action(selected["TestRunner"], item)
    args = test_command["arguments"]
    assert Path(args[0]).name == "test-setup.sh" and Path(args[1]).name == "backtrace_test"
    assert not item["actions"][-1]["cached"]
    env = {p["name"]: p["value"] for p in test_command["environmentVariables"]}
    # Bazel 8.8.1 test-setup.sh uses rlocation(TEST_WORKSPACE / argv[1]).
    runfile = str(Path(env["TEST_SRCDIR"]) / env["TEST_WORKSPACE"] / args[1].removeprefix("./"))
    assert same_digest(input_file(test["inputRootDigest"], runfile)["digest"], binary["digest"])
    item["test_binary_input_verified"] = True
    xml, = [f for f in test_result.get("outputFiles", []) if Path(f["path"]).name == "test.xml"]
    assert xml["digest"]["hash"] == old["xml"]["sha256"] and int(xml["digest"]["sizeBytes"]) == old["xml"]["bytes"]
    item["original_xml_output_verified"] = True
    if name == "gnu":
        assert all(not a["cached"] for a in item["actions"])
    item.update(status="PASS", verified_at=now())


def diagnostics(invocation):
    lines = []
    request = {"selector": {"invocation_id": invocation}}
    for _ in range(12):
        response = json.loads(fetch("api/v1/GetLog", request))
        log = response.get("log", {})
        if log.get("id", {}).get("invocation_id"):
            assert log["id"]["invocation_id"] == invocation
        lines.append(log.get("contents", ""))
        assert sum(map(len, lines)) <= 4 * 1024 * 1024
        token = response.get("nextPageToken", response.get("next_page_token"))
        if not token:
            break
        request["page_token"] = token
    else:
        raise AssertionError("Log exceeds page bound")
    text = "".join(lines)
    # Publish exact known diagnostic categories, never arbitrary authenticated log text.
    patterns = {"disk_full": r"No space left on device|ENOSPC", "out_of_memory": r"OutOfMemoryError|Cannot allocate memory|ran out of memory",
                "execution_log_write": r"(?im)^.*(?:ERROR|FATAL|Exception).*execution log.*$",
                "missing_file": r"(?im)^.*(?:ERROR|FATAL|Exception).*No such file or directory.*$",
                "interrupted": r"(?im)^.*(?:ERROR|FATAL).*interrupted.*$"}
    categories = [name for name, pattern in patterns.items() if re.search(pattern, text)]
    return {"status": "RECOVERED_DIAGNOSTIC" if categories else "NO_ALLOWLISTED_DIAGNOSTIC",
            "bytes": len(text.encode()), "sha256": hashlib.sha256(text.encode()).hexdigest(), "categories": categories}


def main():
    PUBLIC.mkdir(exist_ok=True)
    result = {"status": "INCOMPLETE", "source": SOURCE, "new_builds": 0, "new_tests": 0, "started": now(), "arms": {},
              "scope": "VFIO archive exports and the same archive linked into a passing static cgo test; final ELF symbols recorded separately; no runsc or device traffic qualification"}
    save(result)
    raw = (TMP / "original.zip").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ORIGINAL_SHA
    with zipfile.ZipFile(TMP / "original.zip") as archive:
        assert archive.namelist() == ["result.json"]
        old = json.loads(archive.read("result.json"))
    assert old["source"] == SOURCE and old["status"] == "FAIL"
    result["original"] = old
    result["original_artifact"] = {"id": 13114535148, "sha256": ORIGINAL_SHA}
    save(result)
    for name, invocation in INVOCATIONS.items():
        previous = old["arms"][name]
        assert previous["invocation"] == invocation and previous["completed"] == {OWNER: True, TEST: True}
        attempt, = previous["attempts"]
        assert attempt["status"] == "PASSED" and attempt["strategy"] == "remote" and not attempt["cached"]
        assert previous["xml"]["cases"] == [{"class": "backtrace_test", "name": "TestDumpStack", "status": "PASS"}]
        completed = datetime.fromisoformat(attempt["testAttemptStart"].replace("Z", "+00:00")) + timedelta(milliseconds=int(attempt["testAttemptDurationMillis"]))
        item = {"status": "INCOMPLETE", "original_runtime_status": "PASS", "invocation": invocation,
                "test_completed_at": completed.isoformat(), "original_client_exit": previous["exit"],
                "command_to_test_completion_seconds": (completed - datetime.fromisoformat(previous["command_started"])).total_seconds()}
        result["arms"][name] = item
        try:
            recover(name, previous, item)
        except Exception as error:
            item["collection_error"] = error_info(error)
        save(result)
        try:
            item["original_diagnostic"] = diagnostics(invocation)
        except Exception as error:
            item["original_diagnostic"] = {"status": "INCOMPLETE", "error": error_info(error)}
        save(result)
    states = [a["status"] for a in result["arms"].values()]
    result.update(status="FAIL" if "FAIL" in states else "PASS" if all(s == "PASS" for s in states) else "INCOMPLETE", verified_at=now())
    finished = datetime.fromisoformat(result["verified_at"])
    result["original_run_finish_to_verdict_seconds"] = (finished - datetime.fromisoformat("2026-11-02T03:22:34+00:00")).total_seconds()
    for name, item in result["arms"].items():
        item["test_completion_to_verdict_seconds"] = (finished - datetime.fromisoformat(item["test_completed_at"])).total_seconds()
    save(result)


if __name__ == "__main__":
    main()
