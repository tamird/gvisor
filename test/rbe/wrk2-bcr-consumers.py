"""Qualify the registry LuaJIT with wrk2's existing image and Go consumers."""

from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

TMP = Path(os.environ["RUNNER_TEMP"])
PRIVATE = TMP / "wrk2-private"
PUBLIC = TMP / "wrk2-public"
PRIVATE.mkdir(exist_ok=True)
PUBLIC.mkdir(exist_ok=True)
RC = TMP / "images.bazelrc"
BZ = ["bazel", "--bazelrc=" + str(RC)]
PRODUCT = "1bdd46d07b0388543da20e87e6b17d41fef46e51"
SUPPLIER = "b71adb61cb76def6e12e54b5deb73c1d910f71b5"
REPORT = PUBLIC / "result.json"
RESULT = json.loads(REPORT.read_text()) if REPORT.exists() else {
    "product": PRODUCT, "supplier": SUPPLIER, "carrier": os.environ["GITHUB_SHA"],
    "status": "INCOMPLETE", "phases": {},
    "stock": {"artifact": "https://github.com/tamird/gvisor/actions/runs/41532085303/artifacts/13182183612",
              "sha256": "a3af564b4a1601cf0bdc32f07ce5b552a89548139921b5e93a5acaea918d441c"},
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save() -> None:
    data = json.dumps(RESULT, indent=2).encode() + b"\n"
    secrets = re.findall(r"(?:remote_header|bes_header)=x-buildbuddy-api-key=(\S+)", RC.read_text())
    assert secrets and all(secret.encode() not in data for secret in secrets)
    assert not re.search(rb"(?i)(authorization:|bearer |x-buildbuddy-api-key=|github_pat_|gh[pousr]_[A-Za-z0-9]{20})", data)
    REPORT.write_bytes(data)


def run(name: str, args: list[str], row: dict) -> subprocess.CompletedProcess[str]:
    started = now()
    save()
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    (PRIVATE / (name + ".stdout")).write_text(p.stdout)
    (PRIVATE / (name + ".stderr")).write_text(p.stderr)
    lines = (p.stdout + "\n" + p.stderr).splitlines()
    selected = set()
    for i, line in enumerate(lines):
        if line.startswith(("ERROR:", "FAIL:", "Traceback")) or re.search(r"(?:Error:|error:|AssertionError:)", line):
            selected.update(range(max(0, i - 1), min(len(lines), i + 12)))
    diagnostic = [lines[i] for i in sorted(selected)[:65]
                  if not re.search(r"(?i)(authorization|remote_header|bes_header|x-buildbuddy|bearer)", lines[i])]
    row.setdefault("commands", []).append({
        "command": args, "exit": p.returncode, "started": started, "finished": now(),
        "stdout_sha256": sha(p.stdout.encode()), "stderr_sha256": sha(p.stderr.encode()),
        "invocations": sorted(set(re.findall(r"invocation/([0-9a-f-]{36})", p.stdout + p.stderr))),
        "diagnostics": diagnostic,
    })
    save()
    return p


def phase(name: str, fn: Callable[[dict], None]) -> None:
    row = {"status": "INCOMPLETE", "started": now()}
    RESULT["phases"][name] = row
    try:
        fn(row)
        row["status"] = "PASS"
    except Exception as error:
        row["status"] = "FAIL"
        row["error"] = f"{type(error).__name__}: {error}"[:1800]
    finally:
        row["finished"] = now()
        save()


def supplier(row: dict) -> None:
    assert subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip() == PRODUCT
    changed = subprocess.check_output(["git", "diff", "--name-only", PRODUCT, "HEAD"], text=True).splitlines()
    assert set(changed) == {".github/workflows/build.yml", "test/rbe/wrk2-bcr-consumers.py",
                            "test/rbe/wrk2-image-check.sh", "test/rbe/wrk2-source-check.sh"}, changed
    assert not subprocess.check_output(["git", "diff", "HEAD", "--"], text=True)
    assert "build --registry=" not in RC.read_text()
    assert "common --registry=https://raw.githubusercontent.com/tamird/bazel-central-registry/" + SUPPLIER in RC.read_text()
    p = run("supplier-public", BZ + ["build", "--config=x86_64", "@luajit//:COPYRIGHT", "@luajit//:src/jit/bcsave.lua"], row)
    assert p.returncode == 0
    p = run("output-base", BZ + ["info", "output_base"], row)
    assert p.returncode == 0
    source = Path(p.stdout.strip()) / "external/luajit+"
    registry = Path(os.environ["GITHUB_WORKSPACE"]) / "supplier/modules/luajit/2.1.20230430"
    row["source_sha256"] = {}
    for name in ("BUILD.bazel", "buildvm.bzl"):
        data = (source / name).read_bytes()
        assert data == (registry / "overlay" / name).read_bytes()
        row["source_sha256"][name] = sha(data)
    data = (source / "COPYRIGHT").read_bytes()
    assert sha(data) == "52fc5b15ac968ed81aabb4c1e23ab3bce50f57a5764e9cdca86b463a11072921"
    row["source_sha256"]["COPYRIGHT"] = sha(data)
    row["source_sha256"]["bcsave.lua"] = sha((source / "src/jit/bcsave.lua").read_bytes())
    row["registry_validation"] = json.loads((TMP / "bcr-validation.json").read_text())
    assert row["registry_validation"]["source"] == SUPPLIER
    assert row["registry_validation"]["exit"] == 0


def cli(row: dict, enabled: bool) -> None:
    flag = "--@luajit//:gc64=" + str(enabled).lower()
    args = ["--config=x86_64", flag]
    p = run("cli-build-" + str(enabled), BZ + ["build", *args, "@luajit//:bin/luajit"], row)
    assert p.returncode == 0
    p = run("cli-file-" + str(enabled), BZ + ["cquery", *args, "--output=files", "@luajit//:bin/luajit"], row)
    assert p.returncode == 0
    name, = p.stdout.splitlines()
    data = Path(name).read_bytes()
    assert data[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", data, 18)[0] == 62
    row["elf"] = {"sha256": sha(data), "size": len(data), "machine": 62}
    code = "assert(require('ffi').abi('gc64') == " + str(enabled).lower() + "); assert(select(2,load('OK')):match('OK')); print('gc64=" + str(enabled).lower() + " errors=OK')"
    p = run("cli-exec-" + str(enabled), [name, "-e", code], row)
    row["stdout"] = p.stdout
    assert p.returncode == 0 and p.stdout.strip() == "gc64=" + str(enabled).lower() + " errors=OK"
    p = run("cli-actions-" + str(enabled), BZ + ["aquery", *args, "--output=jsonproto",
            'mnemonic("RunBinary|CppCompile", deps(@luajit//:bin/luajit))'], row)
    assert p.returncode == 0
    graph = json.loads(p.stdout)
    owners = {v["id"]: v["label"] for v in graph["targets"]}
    suffix = "buildvm_linux_x64" + ("" if enabled else "_nogc64")
    actions = [a for a in graph["actions"] if a["mnemonic"] == "RunBinary" and owners[a["targetId"]].endswith("//:lj_vm_s")]
    action, = actions
    assert action["arguments"][0].endswith("/" + suffix), action["arguments"]
    compiled = [a["arguments"] for a in graph["actions"] if a["mnemonic"] == "CppCompile"
                and owners[a["targetId"]].endswith("//:" + suffix)
                and any(v.endswith("src/host/buildvm.c") for v in a["arguments"])]
    assert compiled and all("-D" + ("LUAJIT_ENABLE_GC64" if enabled else "LUAJIT_DISABLE_GC64") in v for v in compiled)
    row["vm_generator"] = {"owner": owners[action["targetId"]], "arguments": action["arguments"], "compiler_arguments": compiled}


def image(row: dict, cpu: str) -> None:
    p = run("image-" + cpu, ["bash", "test/rbe/wrk2-image-check.sh", cpu], row)
    base = TMP / "wrk2" / cpu
    # Retain independently produced inspection and runtime output if a later gate failed.
    for name in ("image", "elf", "notices", "executable.sha256", "bytecode", "workload", "binfmt", "verdict"):
        if (base / name).is_file():
            row[name] = (base / name).read_text()
    if (base / "generators.json").is_file():
        row["generators"] = json.loads((base / "generators.json").read_text())
    assert p.returncode == 0
    row["runtime"] = "native AMD64 GNU/Linux" if cpu == "x86_64" else "ARM64 QEMU user emulation on AMD64 GNU/Linux"
    assert row["verdict"].startswith("PASS ")


def consumer(row: dict) -> None:
    target = "//test/kubernetes/benchmarks/httpbench:wrk2_test"
    bep = PRIVATE / "consumer.bep.json"
    p = run("consumer", BZ + ["test", "--config=x86_64", "--nocache_test_results", "--strategy=TestRunner=local",
            "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.run=^TestWrk2Consumer$", "--test_timeout=90",
            "--build_event_json_file=" + str(bep), target], row)
    row["build_test_exit"] = p.returncode
    events = [json.loads(line) for line in bep.read_text().splitlines()]
    results = [v for v in events if "testResult" in v.get("id", {})]
    event, = results
    assert event["id"]["testResult"]["label"] == target
    result = event["testResult"]
    row["attempt"] = {"id": event["id"]["testResult"], "status": result["status"], "cachedLocally": result.get("cachedLocally", False), "executionInfo": result.get("executionInfo", {})}
    row["bep_sha256"] = sha(bep.read_bytes())
    assert result["status"] == "PASSED" and not result.get("cachedLocally", False)
    assert result["executionInfo"]["strategy"] == "local"
    output, = [v for v in result["testActionOutput"] if v["name"] == "test.xml"]
    uri = urlparse(output["uri"])
    assert uri.scheme == "file"
    data = Path(unquote(uri.path)).read_bytes()
    xml = ET.fromstring(data)
    cases = xml.findall(".//testcase")
    assert len(cases) == 1 and cases[0].get("name") == "TestWrk2Consumer"
    assert not any(xml.findall(".//" + tag) for tag in ("failure", "error", "skipped"))
    row["xml"] = data.decode()
    row["xml_sha256"] = sha(data)
    assert p.returncode == 0


def catalog(row: dict) -> None:
    p = run("catalog", ["bash", "test/rbe/wrk2-source-check.sh"], row)
    base = TMP / "wrk2/licenses"
    for name in ("verify", "fresh-verify", "discovered", "verdict", "checked-in-verify-rc", "fetch-rc", "fresh-verify-rc"):
        if (base / name).is_file():
            row[name] = (base / name).read_text()
    assert p.returncode == 0
    expected = "all 223 dependencies have up-to-date license entries that conform to governance/licensing.yaml"
    assert row["verify"].strip() == expected and row["fresh-verify"].strip() == expected
    row["catalog_sha256"] = sha(Path("tools/licensecheck/dependencies.yaml").read_bytes())


mode = sys.argv[1]
if mode == "supplier":
    phase("supplier", supplier)
    phase("gc64", lambda row: cli(row, True))
    phase("nogc64", lambda row: cli(row, False))
elif mode == "images":
    phase("amd64", lambda row: image(row, "x86_64"))
    phase("arm64", lambda row: image(row, "aarch64"))
elif mode == "consumer":
    phase("consumer", consumer)
elif mode == "catalog":
    phase("catalog", catalog)
elif mode == "finish":
    expected = {"supplier", "gc64", "nogc64", "amd64", "arm64", "consumer", "catalog"}
    RESULT["status"] = "PASS" if set(RESULT["phases"]) == expected and all(v["status"] == "PASS" for v in RESULT["phases"].values()) else "FAIL"
    RESULT["finished"] = now()
else:
    raise ValueError(mode)
save()
print(json.dumps({"status": RESULT["status"], "phases": {k: v["status"] for k, v in RESULT["phases"].items()}}))
if mode == "finish":
    raise SystemExit(RESULT["status"] != "PASS")
