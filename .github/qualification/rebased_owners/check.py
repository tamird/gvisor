"""Read the selected tests and coverage build products for two gVisor PRs."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree

from image_execution import execution_records

WORK = Path(os.environ["RUNNER_TEMP"]) / "owners"
TESTS_HEAD = "fafff7eba5044b4585ea89fde2db7cbfb7861310"
COVER_HEAD = "7f56b080d9f7485957f3514133ffa5f5490a6185"
GNU = "docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a"
UBUNTU = "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
NOGO = "//pkg/atomicbitops:atomicbitops_test_nogo"
TESTS = {
    f"{base}_{arch}": arch
    for base in ("//pkg/atomicbitops:atomicbitops_test", "//test/util:posix_error_test")
    for arch in ("amd64", "arm64")
} | {NOGO: "amd64"}
COVER = [
    prefix + suffix
    for prefix in ("//runsc:runsc", "//runsc/cmd/sentry:gvisor_sentry")
    for suffix in ("", "-race", "_coverage", "_race_coverage")
]
BAZEL = ["bazelisk", "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(WORK / "client.rc")]


def call(name: str, args: list[str], minutes: int) -> int:
    with (WORK / (name + ".log")).open("wb") as log:
        return subprocess.run(
            ["timeout", "--signal=INT", "--kill-after=60s", f"{minutes}m", *BAZEL, *args],
            stdout=log,
            stderr=subprocess.STDOUT,
        ).returncode


def write(name: str, data: dict) -> None:
    (WORK / (name + ".json")).write_text(json.dumps(data, indent=2) + "\n")


def source() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def bep(name: str):
    path = WORK / (name + ".bep")
    if path.exists():
        with path.open() as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


def diagnostics(name: str) -> list[str]:
    classes = set()
    with (WORK / (name + ".log")).open(errors="replace") as stream:
        for line in stream:
            for marker in (
                "strict dependencies",
                "no matching toolchains",
                "no such package",
                "No space left",
                "out of memory",
                "not a multiple of",
                "No matching execution platform",
                "not executable",
                "Exec format error",
                "aarch64-linux-gnu-gcc",
                "x86_64-linux-gnu-gcc",
                "Killed",
                "undeclared inclusion",
                "failed to fetch",
                "undefined reference",
                "timed out",
            ):
                if marker.lower() in line.lower():
                    classes.add(marker)
    return sorted(classes)


def test_report(result: dict, item: dict) -> None:
    for report in result.get("testActionOutput", []):
        name = report.get("name")
        uri = urlparse(report.get("uri", ""))
        if name not in ("test.log", "test.xml"):
            continue
        if uri.scheme == "file":
            path = Path(unquote(uri.path))
            if not path.is_file():
                continue
            raw = path.read_bytes()
        elif uri.scheme == "bytestream":
            blob = re.search(r"/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/([0-9]+)$", uri.path)
            if not blob:
                continue
            owner = item["label"].removeprefix("//").replace(":", "/")
            for candidate in Path("bazel-out").glob("*/testlogs/" + owner + "/" + name):
                if candidate.is_file() and candidate.stat().st_size == int(blob[2]):
                    raw = candidate.read_bytes()
                    if hashlib.sha256(raw).hexdigest() == blob[1]:
                        break
            else:
                continue
        else:
            continue
        item[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        if name == "test.xml":
            cases = list(ElementTree.fromstring(raw).iter("testcase"))
            item["caseCount"] = len(cases)
            if item["label"] != NOGO:
                expected = "TestOrUint32" if item["label"].startswith("//pkg/atomicbitops:") else "PosixError"
                item["namedCase"] = any(
                    case.get("name", "").split("/")[-1].split(".")[-1] == expected for case in cases
                )
            item["nonpassing"] = sum(
                any(c.find(tag) is not None for tag in ("failure", "error", "skipped"))
                or c.get("status") == "notrun"
                for c in cases
            )
        elif item["label"] == NOGO:
            item["analysis"] = {
                arch: bool(re.search(rb"\b" + arch.encode() + rb"\.\.\.PASS", raw)) for arch in ("amd64", "arm64")
            }


def tests() -> bool:
    name = "variants"
    receipt = {"source": source(), "verified": False, "owners": {}, "runners": {}}
    if receipt["source"] != TESTS_HEAD:
        raise ValueError("Unexpected variant source")
    try:
        receipt["exit"] = call(
            name,
            [
                "test",
                "--config=x86_64",
                "-c",
                "opt",
                "--keep_going",
                "--remote_download_outputs=toplevel",
                "--nocache_test_results",
                "--flaky_test_attempts=1",
                "--runs_per_test=1",
                "--test_sharding_strategy=disabled",
                "--test_env=GO_TEST_WRAP=1",
                "--test_env=GO_TEST_WRAP_TESTV=1",
                "--modify_execution_info=^TestRunner$=+no-remote-cache",
                "--strategy=TestRunner=remote",
                "--build_event_json_file_path_conversion=false",
                "--build_event_json_file=" + str(WORK / "variants.bep"),
                "--execution_log_json_file=" + str(WORK / "variants.spawn"),
                *TESTS,
            ],
            28,
        )
        receipt["diagnostics"] = diagnostics(name)
        for event in bep(name):
            identity = event["id"].get("testResult", {})
            result = event.get("testResult")
            label = identity.get("label")
            if label not in TESTS or not result:
                continue
            info = result.get("executionInfo", {})
            item = {
                "label": label,
                "attempt": identity.get("attempt"),
                "status": result["status"],
                "remote": info.get("strategy") == "remote",
                "cached": bool(result.get("cachedLocally") or info.get("cachedRemotely")),
            }
            receipt["owners"].setdefault(label, []).append(item)
            test_report(result, item)
        path = WORK / "variants.spawn"
        if path.is_file():
            with path.open(encoding="utf-8", newline="") as stream:
                for _, action in execution_records(stream):
                    if (
                        not action
                        or action.get("mnemonic") != "TestRunner"
                        or action.get("targetLabel") not in TESTS
                    ):
                        continue
                    props = {p["name"]: p["value"] for p in action.get("platform", {}).get("properties", [])}
                    argv = action.get("commandArgs", [])
                    # Bazel can run another TestRunner to synthesize XML. Only
                    # the test setup or the producer of test.cache_status counts.
                    physical = any(x.endswith("/test-setup.sh") for x in argv) or any(
                        x.endswith("/test.cache_status") for x in action.get("listedOutputs", [])
                    )
                    worker = {
                        "physical": physical,
                        "remote": action.get("runner") == "remote",
                        "cached": bool(action.get("cacheHit")),
                        "exit": action.get("exitCode", 0),
                        "emulated": any("qemu-" in x for x in argv),
                        "arch": props.get("Arch") if props.get("Arch") in ("amd64", "arm64") else None,
                        "image": props.get("container-image")
                        if props.get("container-image") in (GNU, UBUNTU)
                        else None,
                    }
                    receipt["runners"].setdefault(action["targetLabel"], []).append(worker)
        for label, arch in TESTS.items():
            attempts = receipt["owners"].get(label, [])
            if len(attempts) != 1:
                return False
            attempt = attempts[0]
            worker = any(
                w["physical"]
                and w["remote"]
                and not w["cached"]
                and w["exit"] == 0
                and not w["emulated"]
                and w["arch"] == arch
                and w["image"] == (GNU if arch == "amd64" else UBUNTU)
                for w in receipt["runners"].get(label, [])
            )
            if not (
                worker
                and attempt["status"] == "PASSED"
                and attempt["remote"]
                and not attempt["cached"]
                and attempt.get("caseCount", 0)
                and not attempt.get("nonpassing")
                and attempt.get("namedCase", label == NOGO)
                and all(attempt.get("analysis", {"amd64": label != NOGO, "arm64": label != NOGO}).values())
            ):
                return False
        receipt["verified"] = receipt["exit"] == 0
        return receipt["verified"]
    finally:
        write(name, receipt)


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def coverage(arch: str) -> bool:
    name = "coverage-" + arch
    config = {"amd64": "x86_64", "arm64": "aarch64"}[arch]
    machine = {"amd64": 62, "arm64": 183}[arch]
    receipt = {"source": source(), "verified": False, "targets": {}, "binaries": {}}
    if receipt["source"] != COVER_HEAD:
        raise ValueError("Unexpected coverage source")
    try:
        options = ["--config=" + config, "-c", "opt", "--remote_download_outputs=toplevel"]
        receipt["exit"] = call(
            name,
            [
                "build",
                *options,
                "--keep_going",
                "--build_event_json_file_path_conversion=false",
                "--build_event_json_file=" + str(WORK / (name + ".bep")),
                *COVER,
            ],
            40,
        )
        receipt["diagnostics"] = diagnostics(name)
        sets = {}
        for event in bep(name):
            ids = event["id"]
            if "namedSet" in ids:
                sets[ids["namedSet"]["id"]] = event.get("namedSetOfFiles", {})
            if (owner := ids.get("targetCompleted", {})).get("label") in COVER and not owner.get("aspect"):
                data = event.get("completed", {})
                refs = [
                    r["id"]
                    for group in data.get("outputGroup", [])
                    if group["name"] == "default"
                    for r in group.get("fileSets", [])
                ]
                receipt["targets"][owner["label"]] = {"built": data.get("success", False), "sets": refs}
        for label, record in receipt["targets"].items():
            refs = record.pop("sets")
            if not record["built"]:
                continue
            paths = set()
            seen = set()
            while refs:
                ref = refs.pop()
                if ref in seen:
                    continue
                seen.add(ref)
                file_set = sets.get(ref, {})
                refs.extend(r["id"] for r in file_set.get("fileSets", []))
                for f in file_set.get("files", []):
                    uri = urlparse(f.get("uri", ""))
                    path = (
                        Path(unquote(uri.path))
                        if uri.scheme == "file"
                        else Path(*f.get("pathPrefix", []), f["name"])
                    )
                    if path.is_file():
                        paths.add(path)
            if len(paths) != 1:
                record["outputCount"] = len(paths)
                continue
            (path,) = paths
            with path.open("rb") as data:
                header = data.read(64)
            actual = int.from_bytes(header[18:20], "little")
            binary = {
                "elf": header.startswith(b"\x7fELF\x02\x01"),
                "machine": actual,
                "size": path.stat().st_size,
                "sha256": file_hash(path),
            }
            receipt["binaries"][label] = binary
            if label.startswith("//runsc:") and binary["elf"] and actual == machine:
                cmd = [str(path.resolve()), "--version"]
                if arch == "arm64":
                    cmd = ["qemu-aarch64", "-L", "/usr/aarch64-linux-gnu", *cmd]
                proc = subprocess.run(cmd, capture_output=True, timeout=90)
                binary.update(
                    versionExit=proc.returncode,
                    reportedRunsc=b"runsc" in proc.stdout,
                    execution="native" if arch == "amd64" else "qemu-user",
                )
        receipt["verified"] = (
            receipt["exit"] == 0
            and set(receipt["binaries"]) == set(COVER)
            and all(
                b["elf"]
                and b["machine"] == machine
                and (not label.startswith("//runsc:") or (b.get("versionExit") == 0 and b.get("reportedRunsc")))
                for label, b in receipt["binaries"].items()
            )
        )
        return receipt["verified"]
    finally:
        write(name, receipt)


if __name__ == "__main__":
    success = tests() if sys.argv[1] == "variants" else coverage(sys.argv[1])
    sys.exit(0 if success else 1)
