"""Qualify the actual musl proposal, using its unchanged Make build."""
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import traceback
import uuid
import xml.etree.ElementTree as ET

from image_execution import execution_records
import qualification_report

ROOT = Path(os.environ["RUNNER_TEMP"])
PRIVATE = ROOT / "vfio-proposal-private"
PUBLIC = ROOT / "vfio-proposal-evidence"
SOURCE = "92c5bed67c8cac88959b0a4cb64e0b52f92b2ec9"
TEST = "//external/tools/plugin-stack:backtrace_test"
ARCHIVE = "@libpluginstack//:libpluginstack"
PROPS = {"Arch": "amd64", "OSFamily": "linux", "EstimatedMemory": "2GB",
         "container-image": "docker://" + os.environ["GNU"], "workload-isolation-type": "firecracker"}
BAZEL = ["bazel", "--noblock_for_lock", "--bazelrc=" + str(ROOT / "gnu.bazelrc")]


def now():
    return datetime.now(timezone.utc).isoformat()


def save(result):
    (PUBLIC / "result.json").write_text(json.dumps(result, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args]).decode().strip()


def command(args, log, timeout="25m"):
    with log.open("wb") as stream:
        return subprocess.run(["timeout", "--signal=INT", "--kill-after=30s", timeout, *args],
                              stdout=stream, stderr=stream).returncode


def digest(path):
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}


def elf(path):
    headers = subprocess.check_output(["readelf", "-hld", str(path)], text=True)
    data = digest(path)
    data.update(amd64="Advanced Micro Devices X86-64" in headers,
                interpreter=re.findall(r"Requesting program interpreter: (.*)]", headers),
                needed=re.findall(r"\(NEEDED\).*\[(.*?)\]", headers))
    assert data["amd64"] and not data["interpreter"] and not data["needed"]
    return data


def phase(result, name, extra):
    out = PRIVATE / name
    out.mkdir(parents=True)
    item = {"status": "INCOMPLETE", "phase": "selection", "started": now(), "source": SOURCE,
            "flags": extra, "invocation": str(uuid.uuid4())}
    result["arms"][name] = item
    save(result)
    flags = ["--config=x86_64", "--config=plugin-tldk", "-c", "opt", "--jobs=42", *extra]
    expr = "str(target.label) + '\\t' + str([f.path for f in target.files.to_list()])"
    select = [*BAZEL, "cquery", *flags, "--output=starlark", "--starlark:expr=" + expr,
              "set(" + ARCHIVE + " " + TEST + ")"]
    with (out / "selection").open("wb") as stdout, (out / "selection.log").open("wb") as stderr:
        item["selection_exit"] = subprocess.run(["timeout", "10m", *select], stdout=stdout, stderr=stderr).returncode
    if item["selection_exit"]:
        item["status"] = "FAIL"
    save(result)
    assert item["selection_exit"] == 0
    selected = {}
    for line in (out / "selection").read_text().splitlines():
        label, files = line.split("\t")
        assert label not in selected
        selected[label] = ast.literal_eval(files)
    owner, = [label for label in selected if label.endswith("//:libpluginstack")]
    assert set(selected) == {owner, TEST}
    item["selected"] = selected
    item.update(phase="test", command_started=now())
    save(result)
    args = [*BAZEL, "test", *flags, "--invocation_id=" + item["invocation"],
            "--build_tag_filters=", "--test_tag_filters=", "--test_env=GO_TEST_WRAP_TESTV=1",
            "--nocache_test_results", "--modify_execution_info=^TestRunner$=+no-remote-cache",
            "--test_sharding_strategy=disabled", "--test_timeout=60", "--strategy=TestRunner=remote",
            "--remote_download_outputs=toplevel", "--build_event_json_file_path_conversion=false",
            "--remote_download_regex=.*[/]libpluginstack_foreign_cc[/]build_script[.]sh$",
            "--build_event_json_file=" + str(out / "raw.bep"),
            "--execution_log_json_file=" + str(out / "spawns.json"), ARCHIVE, TEST]
    item["exit"] = command(args, out / "test.log")
    item.update(command_finished=now(), phase="collection", status="FAIL" if item["exit"] else "INCOMPLETE")
    save(result)
    events = [json.loads(line) for line in (out / "raw.bep").read_text().splitlines()]
    finishes = [e["finished"] for e in events if "finished" in e]
    finish = finishes[0] if len(finishes) == 1 else {}
    item["finish"] = {k: finish[k] for k in ("overallSuccess", "finishTimeMillis", "finishTime") if k in finish}
    completion = {e["id"]["targetCompleted"]["label"]: e["completed"]["success"]
                  for e in events if "targetCompleted" in e["id"] and "completed" in e}
    item["completed"] = completion
    attempts = [e for e in events if e["id"].get("testResult", {}).get("label") == TEST]
    item["attempts"] = [{"identity": e["id"]["testResult"], "status": e["testResult"]["status"],
                         "cached": e["testResult"].get("cachedLocally", False) or e["testResult"].get("executionInfo", {}).get("cachedRemotely", False),
                         "strategy": e["testResult"].get("executionInfo", {}).get("strategy"),
                         **{k: e["testResult"][k] for k in ("testAttemptStartMillis", "testAttemptDurationMillis", "testAttemptStart", "testAttemptDuration") if k in e["testResult"]}}
                        for e in attempts]
    save(result)
    capture = qualification_report.capture(out / "raw.bep", out / "safe.bep", Path("/"), out / "reports")
    item["capture_errors"] = capture["errors"]
    reports = [r for r in capture["reports"] if r["name"] == "test.xml" and r["identity"]["label"] == TEST]
    if len(reports) == 1:
        xml = out / reports[0]["path"]
        cases = [{"class": case.get("classname"), "name": case.get("name"),
                  "status": "FAIL" if case.find("failure") is not None or case.find("error") is not None else
                  "SKIP" if case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") == "suppressed" else "PASS"}
                 for case in ET.fromstring(xml.read_bytes()).iter("testcase")]
        item["xml"] = {**digest(xml), "cases": cases}
    save(result)
    actions = []
    make_inputs, make_outputs, link_inputs, link_outputs = [], [], [], []
    with (out / "spawns.json").open() as stream:
        for _, spawn in execution_records(stream):
            if spawn is None or spawn.get("targetLabel") not in (owner, TEST):
                continue
            if spawn.get("mnemonic") not in ("CcMakeMakeRule", "GoLink", "TestRunner"):
                continue
            properties = {x["name"]: x["value"] for x in spawn.get("platform", {}).get("properties", [])}
            row = {k: spawn.get(k) for k in ("targetLabel", "mnemonic", "runner", "cacheHit", "digest")}
            row.update(exit=spawn.get("exitCode", 0), status=spawn.get("status", ""),
                       executable=Path(spawn["commandArgs"][0]).name,
                       properties={k: properties.get(k) for k in PROPS})
            actions.append(row)
            if spawn["mnemonic"] == "CcMakeMakeRule":
                make_inputs = spawn["inputs"]
                make_outputs = spawn["actualOutputs"]
            elif spawn["mnemonic"] == "GoLink":
                link_inputs = spawn["inputs"]
                link_outputs = spawn["actualOutputs"]
            elif row["executable"] == "test-setup.sh":
                assert Path(spawn["commandArgs"][1]).name == "backtrace_test"
    item["actions"] = actions
    save(result)
    assert item["exit"] == 0 and finish["overallSuccess"] and all(completion.get(label) for label in selected)
    assert len(attempts) == 1 and item["attempts"][0]["status"] == "PASSED" and not item["attempts"][0]["cached"]
    assert item["attempts"][0]["strategy"] == "remote"
    assert not capture["errors"] and len(reports) == 1
    assert item["xml"]["cases"] == [{"class": "backtrace_test", "name": "TestDumpStack", "status": "PASS"}]
    make, = [a for a in actions if a["mnemonic"] == "CcMakeMakeRule"]
    link, = [a for a in actions if a["mnemonic"] == "GoLink"]
    primary, = [a for a in actions if a["mnemonic"] == "TestRunner" and a["executable"] == "test-setup.sh"]
    assert not primary["cacheHit"] and primary["runner"] == "remote"
    assert all(not a["exit"] and not a["status"] and a["properties"] == PROPS for a in actions)
    assert all(a["runner"] == "remote" or (a["cacheHit"] and a["runner"] == "remote cache hit") for a in actions)
    execroot = Path(subprocess.check_output([*BAZEL, "info", "execution_root"], text=True).strip())
    archive, = [execroot / f for f in selected[owner] if Path(f).name == "libpluginstack.a"]
    binary, = [execroot / f for f in selected[TEST] if Path(f).name == "backtrace_test"]
    symbols = subprocess.check_output(["nm", "--defined-only", str(archive)], text=True, stderr=subprocess.DEVNULL)
    required = ["pci_vfio_read_config", "pci_vfio_map_resource", "rte_vfio_container_dma_map"]
    item["archive"] = {**digest(archive), "defined_symbols": [s for s in required if re.search(r"(?m)\b" + s + r"$", symbols)]}
    item["binary"] = elf(binary)
    assert item["archive"]["defined_symbols"] == required
    for records, path, expected in [(make_outputs, archive, item["archive"]),
                                    (link_inputs, archive, item["archive"]),
                                    (link_outputs, binary, item["binary"])]:
        record, = [f for f in records if f["path"] == str(path.relative_to(execroot))]
        assert record["digest"]["hash"] == expected["sha256"]
        assert int(record["digest"]["sizeBytes"]) == expected["bytes"]
    # The generated script is an original action input written by foreign_cc.
    script_input, = [f for f in make_inputs if f["path"].endswith("/libpluginstack_foreign_cc/build_script.sh")]
    script = execroot / script_input["path"]
    text = script.read_text()
    item["make_script"] = {**digest(script), "musl_target": "x86_64-linux-musl" in text,
                           "largefile_aliases": "EXTRA_CPPFLAGS=-D_LARGEFILE64_SOURCE" in text,
                           "vfio_opt_out": "RTE_EAL_VFIO=n" in text}
    compiler = "clang" if name == "musl" else "gcc"
    assert "x86_64-native-linuxapp-" + compiler in text
    item["make_script"]["compiler"] = compiler
    assert not item["make_script"]["vfio_opt_out"]
    assert item["make_script"]["musl_target"] == (name == "musl")
    assert item["make_script"]["largefile_aliases"] == (name == "musl")
    assert item["make_script"]["sha256"] == script_input["digest"]["hash"]
    assert item["make_script"]["bytes"] == int(script_input["digest"]["sizeBytes"])
    assert git("rev-parse", "HEAD") == SOURCE and not git("diff", "HEAD", "--")
    item.update(status="PASS", phase="complete", verdict_time=now())
    save(result)


def main():
    PRIVATE.mkdir(exist_ok=True)
    PUBLIC.mkdir(exist_ok=True)
    result = {"status": "INCOMPLETE", "source": SOURCE, "base": "ccae8886eed54388bb6a36dd3211a6e5831ecc5b",
              "scope": "ordinary proposal Make/archive/cgo backtrace on native populated GNU workers; no device traffic or scratch claim",
              "started": now(), "arms": {}}
    save(result)
    assert git("rev-parse", "HEAD") == SOURCE and not git("diff", "HEAD", "--")
    for name, flags in [("gnu", []), ("musl", ["--platforms=@llvm//platforms:linux_x86_64_musl",
                                               "--extra_toolchains=@llvm//toolchain:linux_x86_64_to_linux_x86_64"])]:
        try:
            phase(result, name, flags)
        except Exception as error:
            item = result["arms"][name]
            item["collection_error"] = {"type": type(error).__name__, "line": traceback.extract_tb(error.__traceback__)[-1].lineno}
            save(result)
    states = [a["status"] for a in result["arms"].values()]
    result.update(status="FAIL" if "FAIL" in states else "PASS" if states == ["PASS", "PASS"] else "INCOMPLETE", verdict_time=now())
    save(result)


if __name__ == "__main__":
    main()
