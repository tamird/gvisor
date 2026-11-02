"""Replay the established two-architecture Hackbench image/Go consumer proof."""

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import xml.etree.ElementTree as ET

PRODUCT = "7c63124a29588f4cdc264e11b8f345230c6efed3"
COPYING = "ab15fd526bd8dd18a9e77ebc139656bf4d33e97fc7238cd11bf60e2b9b8666c6"
TMP = Path(os.environ["RUNNER_TEMP"])
PRIVATE = TMP / "hackbench-private"
PUBLIC = TMP / "hackbench-public"
PRIVATE.mkdir(exist_ok=True)
PUBLIC.mkdir(exist_ok=True)
RESULT = {"product": PRODUCT, "carrier": os.environ["GITHUB_SHA"], "phases": {}}
BZ = ["bazel", "--bazelrc=" + str(TMP / "images.bazelrc")]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save() -> None:
    data = json.dumps(RESULT, indent=2).encode() + b"\n"
    for key, value in os.environ.items():
        if ("TOKEN" in key or "SECRET" in key or "API_KEY" in key) and len(value) >= 12:
            assert value.encode() not in data, f"Secret in report: {key}"
    assert b"x-buildbuddy-api-key" not in data and b"Bearer " not in data
    (PUBLIC / "result.json").write_bytes(data)


def run(name: str, args: list[str], row: dict) -> subprocess.CompletedProcess:
    started = now()
    p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = PRIVATE / (name + ".log")
    log.write_text(p.stdout)
    item = {"command": args, "started": started, "finished": now(), "exit": p.returncode,
            "log_sha256": sha(log.read_bytes())}
    row.setdefault("commands", []).append(item)
    if p.returncode:
        # Only ordinary build diagnostics, never full logs or environment dumps.
        item["diagnostics"] = [line[:700] for line in p.stdout.splitlines()
                               if line.startswith(("ERROR:", "FAIL:", "licensecheck:"))
                               and not any(word in line.lower() for word in
                                           ("authorization", "token", "api-key", "https://"))][:10]
    save()
    return p


def checked(name: str, args: list[str], row: dict) -> str:
    p = run(name, args, row)
    assert p.returncode == 0, f"{name} exited {p.returncode}"
    return p.stdout


def output(name: str, target: str, flags: list[str], row: dict) -> Path:
    lines = checked(name, BZ + ["cquery", *flags, "--output=files", target], row).splitlines()
    files = [Path(line) for line in lines if line.startswith("bazel-out/")]
    assert len(files) == 1 and files[0].is_file(), files
    return files[0]


def image(arch: str, cpu: str, machine: int, row: dict) -> None:
    flags = ["--config=" + arch]
    target = "//images/benchmarks/hackbench:hackbench_context"
    checked(arch + "-build", BZ + ["build", *flags, target], row)
    archive = output(arch + "-path", target, flags, row)
    row["context"] = {"size": archive.stat().st_size, "sha256": sha(archive.read_bytes())}
    with tarfile.open(archive) as tar:
        members = [m for m in tar.getmembers() if m.name.removeprefix("./") ==
                   "images/benchmarks/hackbench/image.tar"]
        assert len(members) == 1 and members[0].isfile(), members
        f = tar.extractfile(members[0])
        assert f is not None
        saved = PRIVATE / (arch + "-image.tar")
        saved.write_bytes(f.read())
    checked(arch + "-load", ["docker", "load", "-i", str(saved)], row)
    tag = "gvisor.dev/images/benchmarks/hackbench:latest"
    info = json.loads(checked(arch + "-inspect", ["docker", "image", "inspect", tag], row))[0]
    assert info["Architecture"] == cpu and info["Os"] == "linux", info["Architecture"]
    row["image"] = {"id": info["Id"], "architecture": cpu, "os": info["Os"],
                    "layers": info["RootFS"]["Layers"]}
    cid = checked(arch + "-create", ["docker", "create", tag, "--help"], row).strip()
    try:
        binary = PRIVATE / (arch + "-hackbench")
        notice = PRIVATE / (arch + "-COPYING")
        checked(arch + "-elf-copy", ["docker", "cp", cid + ":/usr/bin/hackbench", str(binary)], row)
        checked(arch + "-license-copy", ["docker", "cp", cid + ":/usr/share/doc/hackbench/COPYING", str(notice)], row)
        data = binary.read_bytes()
        assert data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[18:20], "little") == machine
        elf = checked(arch + "-elf", ["readelf", "-l", "-d", str(binary)], row)
        assert "INTERP" not in elf and "(NEEDED)" not in elf, "ELF is not static"
        assert sha(notice.read_bytes()) == COPYING
        row["elf"] = {"size": len(data), "sha256": sha(data), "machine": machine, "static": True}
        row["copying_sha256"] = COPYING
        help_text = checked(arch + "-help", ["docker", "start", "-a", cid], row)
        assert help_text.splitlines()[0] == "hackbench V 2.11", help_text[:100]
        row["version"] = help_text.splitlines()[0]
    finally:
        subprocess.run(["docker", "rm", "-f", cid], stdout=subprocess.DEVNULL, check=True)
    target = "//test/benchmarks/base:hackbench_test"
    bep = PRIVATE / (arch + "-benchmark.bep.json")
    args = BZ + ["test", "--jobs=2", "--strategy=TestRunner=local", "--nocache_test_results",
                 "--test_output=errors", "--test_arg=-test.v", "--test_arg=-test.run=^$",
                 "--test_arg=-test.bench=^BenchmarkHackbench$", "--test_arg=-test.benchtime=1x",
                 "--test_arg=-runtime=runc", "--test_timeout=600", "--build_event_json_file=" + str(bep), target]
    p = run(arch + "-benchmark", args, row)
    base = Path("bazel-testlogs/test/benchmarks/base/hackbench_test")
    log = (base / "test.log").read_text() if (base / "test.log").exists() else ""
    row["benchmark_exit"] = p.returncode
    assert p.returncode == 0, "Real benchmark failed"
    events = [json.loads(line) for line in bep.read_text().splitlines()]
    attempts = [e for e in events if "testResult" in e.get("id", {})]
    assert len(attempts) == 1 and attempts[0]["id"]["testResult"]["label"] == target
    result = attempts[0]["testResult"]
    assert result["status"] == "PASSED" and not result.get("cachedLocally", False)
    assert result.get("executionInfo", {}).get("strategy") == "local", result.get("executionInfo")
    row["attempt"] = {"id": attempts[0]["id"]["testResult"], "status": result["status"],
                      "execution": result.get("executionInfo"), "cachedLocally": False}
    xml = (base / "test.xml").read_bytes()
    root = ET.fromstring(xml)
    assert not root.findall(".//failure") and not root.findall(".//error") and not root.findall(".//skipped")
    assert "--- FAIL:" not in log and "--- SKIP:" not in log
    rows = []
    for case in ("BenchmarkHackbench/ipcMode.pipe/processMode.thread", "BenchmarkHackbench/ipcMode.socket/processMode.process"):
        found = [s for s in log.splitlines() if s.startswith(case) and "execution_time_per_loop" in s]
        assert len(found) == 1, (case, found)
        rows.extend(found)
    row["benchmark_rows"] = rows
    row["xml_sha256"] = sha(xml)
    row["log_sha256"] = sha(log.encode())
    row["runtime"] = "native AMD64 runc" if cpu == "amd64" else "QEMU ARM64 under AMD64 runc"


def catalog(row: dict) -> None:
    os.environ["MAKEFLAGS"] = "DOCKER_BUILD=false STARTUP_OPTIONS=--bazelrc=" + str(TMP / "images.bazelrc")
    text = checked("catalog", BZ + ["run", "//tools/licensecheck/main:licensecheck", "--", "--mode=verify"], row)
    lines = [line for line in text.splitlines() if re.fullmatch(r"all \d+ dependencies have up-to-date license entries that conform to governance/licensing.yaml", line)]
    assert len(lines) == 1, "Canonical catalog success line missing"
    row["verification"] = lines[0]


def phase(name: str, fn: Callable[[dict], None]) -> None:
    row = {"started": now(), "status": "INCOMPLETE"}
    RESULT["phases"][name] = row
    try:
        fn(row)
        row["status"] = "PASS"
    except Exception as e:
        row["status"] = "FAIL"
        row["error"] = f"{type(e).__name__}: {e}"[:1200]
    finally:
        row["finished"] = now()
        save()


assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == PRODUCT
assert not subprocess.check_output(["git", "diff", "HEAD", "--"], text=True)
RESULT["started"] = now()
phase("amd64", lambda row: image("x86_64", "amd64", 62, row))
phase("arm64", lambda row: image("aarch64", "arm64", 183, row))
phase("catalog", catalog)
RESULT["status"] = "PASS" if all(row["status"] == "PASS" for row in RESULT["phases"].values()) else "FAIL"
RESULT["finished"] = now()
save()
print(json.dumps({"status": RESULT["status"], "phases": {k: v["status"] for k, v in RESULT["phases"].items()}}))
raise SystemExit(RESULT["status"] != "PASS")
