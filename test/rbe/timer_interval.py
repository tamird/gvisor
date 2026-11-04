"""Check the declared timer link and one existing Linux/gVisor GTest."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import traceback
from urllib.parse import unquote, urlsplit
import uuid
import xml.etree.ElementTree as ET

import continuous
from stdlib_provenance import fields, first, number

ROOT = Path(os.environ["RUNNER_TEMP"])
continuous.ROOT = ROOT
TARGET = "//test/syscalls/linux:timers_test"
GUEST = "//test/syscalls:timers_test_runsc_systrap_directfs"
CASE = "IntervalTimerTest.NegativeInterval"
SOURCE = "f876b5047538c0bbe2de38ef3f306a2cab985aa7"
BASE = "588e6f3436b08e9b9a49e656458d9ea6178bfedd"
COMPILER = "external/llvm++llvm_toolchain_minimal+llvm-toolchain-minimal-linux-amd64/bin/clang++"
RESULT = {"source": SOURCE, "master_fix": "626ca2e773c6e35a4948594927ca9a0e3ab3f1dd",
          "original_source": BASE, "original_artifact": "13347734045",
          "original_sha256": "f83548d8d079b4f49ddaaf57f8af29695b1c64554eaa2575260226cee76503e1",
          "expected": ["native", "systrap"], "kernel": platform.release(), "phases": {}}


def save():
    RESULT["verified_at"] = datetime.now(timezone.utc).isoformat()
    RESULT["missing"] = [name for name in RESULT["expected"] if name not in RESULT["phases"]]
    RESULT["failed"] = [n for n, r in RESULT["phases"].items() if r["status"] == "FAIL"]
    RESULT["status"] = ("FAIL" if RESULT["failed"] else "INCOMPLETE" if RESULT["missing"] or
                        any(r["status"] != "PASS" for r in RESULT["phases"].values()) else "PASS")
    continuous.write(ROOT / "out/verdict.json", RESULT)


def verify_gtest(row, xml, console):
    row["xml_sha256"] = continuous.digest(xml)
    cases = list(ET.parse(xml).getroot().iter("testcase"))
    row["named_cases"] = sum((node.get("classname"), node.get("name")) ==
                             tuple(CASE.split(".")) for node in cases)
    row["case_count"] = len(cases)
    row["started_case"] = bool(re.search(r"\[\s*RUN\s*\]\s+" + re.escape(CASE) + r"\b", console))
    row["passed_case"] = bool(re.search(r"\[\s*OK\s*\]\s+" + re.escape(CASE) + r"\b", console))
    node, = cases
    assert row["named_cases"] == 1 and row["started_case"] and row["passed_case"]
    assert node.get("status") != "notrun" and node.get("result") != "suppressed"
    assert all(node.find(tag) is None for tag in ("failure", "error", "skipped"))


def verify_link(row, events, requested):
    links = []
    with subprocess.Popen(["zstd", "-qdc", ROOT / "link.compact"], stdout=subprocess.PIPE) as child:
        while (size := number(child.stdout)) is not None:
            assert 0 < size < 64 * 1024**2
            entry = fields(child.stdout.read(size))
            if 2 in entry:
                row["compact_invocation"] = first(fields(first(entry, 2)), 4).decode()
            if 7 not in entry:
                continue
            spawn = fields(first(entry, 7))
            if continuous.normalized(first(spawn, 7).decode()) != TARGET or first(spawn, 8) != b"CppLink":
                continue
            selected = first(spawn, 1).decode()
            links.append({"declared_driver": selected if selected == COMPILER else None,
                          "runner": first(spawn, 11).decode(), "cached": bool(first(spawn, 12, 0)),
                          "exit": first(spawn, 9, 0)})
        assert child.wait() == 0
    row["links"] = links
    expected = "bazel-out/k8-fastbuild/bin/test/syscalls/linux/timers_test"
    actions = [(e["id"]["actionCompleted"], e["action"]) for e in events if e.get("action", {}).get("type") == "CppLink"
               and continuous.normalized(e["id"]["actionCompleted"].get("label", "")) == TARGET]
    row["target_action_outputs"] = [k.get("primaryOutput") for k, _ in actions]
    key, action = next((k, a) for k, a in actions if k.get("primaryOutput") == expected)
    row["output"] = key["primaryOutput"]
    row["link_configuration"] = key["configuration"]["id"]
    assert action["success"] and len(links) == 1 and row["compact_invocation"] == requested
    assert links[0] == {"declared_driver": COMPILER, "runner": "linux-sandbox", "cached": False, "exit": 0}
    binary = Path(expected).resolve(strict=True)
    assert binary == Path("bazel-bin/test/syscalls/linux/timers_test").resolve(strict=True)
    binary.relative_to((Path.home() / ".cache/bazel").resolve(strict=True))
    with binary.open("rb") as stream:
        header = stream.read(20)
    row["elf_machine"] = int.from_bytes(header[18:20], "little")
    row["elf_sha256"] = continuous.digest(binary)
    assert header[:6] == b"\x7fELF\x02\x01" and row["elf_machine"] == 62 and os.access(binary, os.X_OK)


def execute(name):
    native = name == "native"
    target = TARGET if native else GUEST
    bep = ROOT / (name + ".jsonl")
    uuid_ = str(uuid.uuid4())
    row = RESULT["phases"][name] = {"target": target, "status": "INCOMPLETE", "invocation": uuid_}
    save()
    args = ["run" if native else "test", "--jobs=4", "--color=no", "--curses=no",
            "--build_tag_filters=-network_plugins", "--remote_executor=", "--remote_cache=", "--bes_backend=",
            "--build_event_json_file=" + str(bep), "--build_event_json_file_path_conversion=false",
            "--invocation_id=" + uuid_]
    if native:
        xml = ROOT / "native.xml"
        args += ["--build_event_publish_all_actions", "--execution_log_compact_file=" + str(ROOT / "link.compact"),
                 target, "--", "--gtest_filter=" + CASE, "--gtest_output=xml:" + str(xml)]
    else:
        args += ["--nocache_test_results", "--flaky_test_attempts=1", "--test_sharding_strategy=disabled",
                 "--strategy=TestRunner=linux-sandbox", "--sandbox_add_mount_pair=/tmp",
                 "--test_timeout=240", "--test_filter=" + CASE, target]
    console = ROOT / (name + ".log")
    with console.open("wb") as stream:
        code = subprocess.run(["timeout", "-k", "20", "1200" if native else "2700",
                               ROOT / "bazel", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g",
                               *args], stdout=stream, stderr=subprocess.STDOUT).returncode
    row.update(native_exit=code, execution_finished_at=continuous.now(), status="FAIL" if code else "INCOMPLETE")
    save()
    text = console.read_text(errors="replace")
    row["console_sha256"] = continuous.digest(console)
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("ERROR:"):
            row["first_bazel_error"] = continuous.diagnostic("\n".join(lines[index:index + 10]))
            break
    events = list(continuous.events(bep))
    row["test_attempts"] = sum("testResult" in e for e in events)
    save()
    assert [e["started"]["uuid"] for e in events if "started" in e] == [uuid_]
    if native:
        for check, verify, arguments in (("link", verify_link, (row, events, uuid_)),
                                         ("case", verify_gtest, (row, xml, text))):
            try:
                verify(*arguments)
            except (AssertionError, KeyError, OSError, StopIteration, ValueError, ET.ParseError) as err:
                row[check + "_error"] = {"type": type(err).__name__,
                                         "line": traceback.extract_tb(err.__traceback__)[-1].lineno}
            save()
        assert "link_error" not in row and "case_error" not in row
    else:
        attempt, = [e for e in events if "testResult" in e]
        assert attempt["id"]["testResult"]["label"] == GUEST
        result = attempt["testResult"]
        info = result["executionInfo"]
        row.update(test_status=result["status"], strategy=info.get("strategy"),
                   cached=bool(result.get("cachedLocally") or info.get("cachedRemotely")))
        files = {}
        for item in result["testActionOutput"]:
            if item["name"] not in ("test.xml", "test.log"):
                continue
            url = urlsplit(item["uri"])
            assert url.scheme == "file" and not url.netloc
            path = Path(unquote(url.path)).resolve(strict=True)
            relative = Path("test/syscalls/timers_test_runsc_systrap_directfs") / item["name"]
            assert path == (Path("bazel-testlogs") / relative).resolve(strict=True)
            path.relative_to((Path.home() / ".cache/bazel").resolve(strict=True))
            assert path.is_file() and 0 < path.stat().st_size < 16 * 1024**2
            files[item["name"]] = path
        log = files["test.log"]
        row["log_sha256"] = continuous.digest(log)
        test_log = log.read_text(errors="replace")
        row["runner_diagnostics"] = [term for term in ("creating default loopback interface", "connection reset by peer",
            "permission denied", "operation not permitted", "unshare", "SIGSYS", "segmentation fault",
            "device or resource busy", "panic:", "failed to start", "No such file or directory") if term in test_log]
        save()
        verify_gtest(row, files["test.xml"], test_log)
        assert not row["cached"] and row["strategy"] == "linux-sandbox" and row["test_status"] == "PASSED"
    assert code == 0
    row["status"] = "PASS"


save()
assert continuous.git("rev-parse", "HEAD") == SOURCE and continuous.git("rev-parse", "HEAD^") == BASE
assert continuous.git("rev-parse", "HEAD:test/syscalls/linux/timers.cc") == "f9be9a7df51325a66bb5655a94174cec0fe01a97"
assert continuous.git("diff", "--name-only", BASE, SOURCE) == "test/syscalls/linux/timers.cc"
assert continuous.git("status", "--porcelain", "--untracked-files=no") == ""
for name in RESULT["expected"]:
    try:
        execute(name)
    except Exception as err:
        row = RESULT["phases"][name]
        row["verification_error"] = {"type": type(err).__name__, "line": traceback.extract_tb(err.__traceback__)[-1].lineno}
        if row.get("native_exit") not in (None, 0):
            row["status"] = "FAIL"
    finally:
        save()
    if name == "native" and RESULT["phases"][name].get("native_exit") != 0:
        break
raise SystemExit(int(RESULT["status"] != "PASS"))
