"""Verify the existing Clang supplier, detached parser test, and embedded Go archive."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(os.environ["RUNNER_TEMP"]) / "parser-cost"
SOURCE = "5967bbde193112cce575c5443c6b241d99589f59"
OWNER = "//tools/nvidia_driver_differ:driver_ast_parser_test"
TOOL = "//tools/nvidia_driver_differ:run_differ"
ARCHIVE = "//tools/nvidia_driver_differ/parser:builtin_headers"
IID = str(uuid.uuid4())
REPORT = ROOT / "report.json"


def save(data):
    REPORT.write_text(json.dumps(data, indent=2) + "\n")


def check():
    data = {"source": SOURCE, "invocation": IID, "complete": False}
    save(data)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    bep = ROOT / "build.bep"
    log = ROOT / "build.log"
    image = "78d4744ead1f5b19b04d0df558ab182a5300903900755e515b2ca4d10eb2044d"
    cmd = [
        "docker", "exec", "gnu", str(ROOT / "bazel"), "--nosystem_rc", "--nohome_rc",
        "--bazelrc=" + str(ROOT / "private.rc"), "test", "--config=x86_64", "-c", "opt",
        "--inject_repository=tcp_gnu=" + str(ROOT / "platform"),
        "--extra_execution_platforms=@tcp_gnu//:gnu", "--host_platform=@tcp_gnu//:gnu",
        "--lockfile_mode=off", "--noshow_progress", "--color=no", "--curses=no",
        "--use_target_platform_for_tests", "--jobs=90", "--remote_retries=1",
        "--noremote_local_fallback", "--strategy=TestRunner=remote",
        "--remote_default_exec_properties=Arch=amd64", "--remote_default_exec_properties=OSFamily=linux",
        "--remote_default_exec_properties=workload-isolation-type=oci",
        "--remote_default_exec_properties=container-image=docker://mirror.gcr.io/library/ubuntu@sha256:" + image,
        "--nocache_test_results", "--nobuild_tests_only", "--test_sharding_strategy=disabled",
        "--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1",
        "--remote_download_outputs=toplevel", "--build_event_json_file=" + str(bep),
        "--invocation_id=" + IID, OWNER, TOOL, ARCHIVE,
    ]
    with log.open("wb") as out:
        run = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT, timeout=6300)
    data["bazel_exit"] = run.returncode
    data["targets"] = {}
    data["attempts"] = []
    if bep.exists():
        for line in bep.open():
            event = json.loads(line)
            identity = event["id"]
            if (label := identity.get("targetCompleted", {}).get("label")) in (OWNER, TOOL, ARCHIVE) and "completed" in event:
                data["targets"][label] = event["completed"].get("success", False)
            if identity.get("testResult", {}).get("label") == OWNER and "testResult" in event:
                result = event["testResult"]
                execution = result.get("executionInfo", {})
                data["attempts"].append({"status": result["status"], "strategy": execution.get("strategy", ""),
                                         "cached": result.get("cachedLocally", False) or execution.get("cachedRemotely", False)})
    messages = log.read_bytes().lower()
    diagnostics = ("no such target", "visibility", "failed remote action", "out of memory", "disk quota exceeded",
                   "no space left", "failed to create process", "file not found", "undefined reference",
                   "failed to fetch", "fatal error", "undeclared inclusion", "exit 137", "testparser")
    data["diagnostics"] = [x for x in diagnostics if x.encode() in messages]
    save(data)
    assert not run.returncode
    assert data["targets"] == {OWNER: True, TOOL: True, ARCHIVE: True}, data["targets"]
    assert data["attempts"] == [{"status": "PASSED", "strategy": "remote", "cached": False}], data["attempts"]
    test_dir = "bazel-testlogs/tools/nvidia_driver_differ/driver_ast_parser_test/"
    xml = subprocess.check_output(["docker", "exec", "gnu", "cat", test_dir + "test.xml"])
    data["xml_sha256"] = hashlib.sha256(xml).hexdigest()
    cases = ET.fromstring(xml).findall(".//testcase")
    data["cases"] = [r.get("name") for r in cases]
    assert data["cases"] == ["TestParser"]
    assert all(r.find(tag) is None for r in cases for tag in ("failure", "error", "skipped"))
    validation = """
import hashlib, json, mmap, pathlib, zipfile
p=pathlib.Path("bazel-bin/tools/nvidia_driver_differ")
archive=p/"parser/builtin_headers.zip"
raw=archive.read_bytes()
with zipfile.ZipFile(archive) as z:
 names=z.namelist()
 required={"include/stddef.h","include/stdint.h","CLANG-LICENSE.TXT"}
 assert required.issubset(names) and len(names)==len(set(names))
 license_sha=hashlib.sha256(z.read("CLANG-LICENSE.TXT")).hexdigest()
 assert license_sha=="ebcd9bbf783a73d05c53ba4d586b8d5813dcdf3bbec50265860ccc885e606f47"
runner=next(x for x in [p/"run_differ",p/"run_differ_/run_differ"] if x.is_file())
with runner.open("rb") as f, mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as elf:
 assert elf[:6]==bytes.fromhex("7f454c460201") and int.from_bytes(elf[18:20],"little")==62
 embedded=elf.find(raw)>=0
 assert embedded
 print(json.dumps({"members":len(names),"archive_sha256":hashlib.sha256(raw).hexdigest(),"license_sha256":license_sha,
 "archive_bytes":len(raw),"complete_archive_embedded":embedded,"go_tool_bytes":len(elf)}))
"""
    data["package"] = json.loads(subprocess.check_output(["docker", "exec", "gnu", "python3", "-c", validation]))
    data["complete"] = True
    save(data)


if __name__ == "__main__":
    check()
