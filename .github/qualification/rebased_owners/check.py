"""Read native test results for the corrected Bazel 9 execution groups."""

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
STOCK = "fafff7eba5044b4585ea89fde2db7cbfb7861310"
TESTS_HEAD = "f3fa3075e29e3dba659ccc64d16378a2ad2b49fb"
GNU = "docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a"
UBUNTU = "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
TESTS = {
    "//pkg/atomicbitops:atomicbitops_test_arm64": "arm64",
    "//test/util:posix_error_test_amd64": "amd64",
    "//test/util:posix_error_test_arm64": "arm64",
}
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
                "Unrecognized option",
                "no such target",
                "unknown exec group",
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
            if item["label"] in TESTS:
                expected = "TestOrUint32" if item["label"].startswith("//pkg/atomicbitops:") else "PosixError"
                item["namedCase"] = any(
                    case.get("name", "").split("/")[-1].split(".")[-1] == expected for case in cases
                )
            item["nonpassing"] = sum(
                any(c.find(tag) is not None for tag in ("failure", "error", "skipped"))
                or c.get("status") == "notrun"
                for c in cases
            )
        elif name == "test.log":
            match = re.search(rb"(?m)^NATIVE_TEST_WORKER=(x86_64|aarch64)\r?$", raw)
            item["uname"] = match[1].decode() if match else None


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
                """--run_under=/bin/sh -c 'echo NATIVE_TEST_WORKER=$(/bin/uname -m); exec "$@"' native-test""",
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
            50,
        )
        receipt["diagnostics"] = diagnostics(name)
        for event in bep(name):
            ident = event["id"]
            invocation = event.get("started", {}).get("uuid", "")
            if re.fullmatch(r"[a-f0-9-]{36}", invocation):
                receipt["invocation"] = invocation
            action = event.get("action", {})
            if action and not action.get("success", False) and "actionCompleted" in ident:
                raw = action.get("stderr", {})
                blob = re.search(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/(\d+)$", raw.get("uri", ""))
                label = ident["actionCompleted"].get("label", "")
                mnemonic = action.get("type")
                rows = receipt.setdefault("failedActions", [])
                if len(rows) < 12:
                    rows.append({"owner": label if re.fullmatch(r"//(?:test/util|pkg/atomicbitops):[a-zA-Z0-9_.-]{1,100}", label) else "dependency",
                                 "type": mnemonic if mnemonic in {"TestRunner", "CppCompile", "CppLink", "GoCompilePkg", "GoLink"} else "other",
                                 "exit": action.get("exitCode"), "stderr": [blob[1], int(blob[2])] if blob else None})
            identity = ident.get("testResult", {})
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
                and attempt.get("namedCase")
                and attempt.get("uname") == {"arm64": "aarch64", "amd64": "x86_64"}[arch]
            ):
                return False
        receipt["verified"] = receipt["exit"] == 0
        return receipt["verified"]
    finally:
        write(name, receipt)


def platforms(arm):
    assert source() == {"stock": STOCK, "fixed": TESTS_HEAD}[arm]
    name = "platforms-" + arm
    report = {"source": source(), "platforms": {}}
    try:
        pattern = 'mnemonic("TestRunner", set(' + " ".join(TESTS) + "))"
        args = ["aquery", "--config=x86_64", "-c", "opt", "--keep_going",
                "--output=jsonproto", "--noinclude_commandline", "--noinclude_artifacts", pattern]
        with (WORK / (name + ".aq")).open("wb") as output, (WORK / (name + ".log")).open("wb") as log:
            process = subprocess.run(["timeout", "15m", *BAZEL, *args], stdout=output, stderr=log)
        report["exit"] = process.returncode
        report["diagnostics"] = diagnostics(name)
        path = WORK / (name + ".aq")
        if process.returncode or path.stat().st_size > 64 * 1024 * 1024:
            return
        graph = json.loads(path.read_bytes())
        labels = {str(target["id"]): target["label"] for target in graph.get("targets", [])}
        for action in graph.get("actions", []):
            owner = labels.get(str(action.get("targetId")))
            if owner in TESTS and action.get("mnemonic") == "TestRunner":
                platform = action.get("executionPlatform", "")
                cpu = next((arch for suffix, arch in [("//:gnu_amd64", "amd64"), ("//:native_arm64", "arm64")]
                            if "rebased_workers" in platform and platform.endswith(suffix)), "other")
                if cpu not in report["platforms"].setdefault(owner, []):
                    report["platforms"][owner].append(cpu)
    finally:
        write(name, report)


if __name__ == "__main__":
    if sys.argv[1] == "platforms":
        platforms(sys.argv[2])
    else:
        raise SystemExit(0 if tests() else 1)
