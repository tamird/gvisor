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

PHASE = sys.argv[1]
if PHASE not in ("positive", "macro"):
    raise ValueError("unknown qualification phase")
ROOT = Path(os.environ["RUNNER_TEMP"]) / "owners"
WORK = ROOT / PHASE
TESTS_HEAD = {"positive": "da88f239eb2f37f2a58db6c3a70296c562ed006a",
              "macro": "3154c069697c7606af448e2d98c870187dd8bbea"}[PHASE]
GNU = "docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a"
UBUNTU = "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
TESTS = {"//test/util:posix_error_test_arm64": "arm64"}
if PHASE == "positive":
    TESTS.update({
        "//test/util:posix_error_test_amd64": "amd64",
        "//pkg/atomicbitops:atomicbitops_test_amd64": "amd64",
        "//pkg/atomicbitops:atomicbitops_test_arm64": "arm64",
    })
BAZEL = ["bazelisk", "--nosystem_rc", "--nohome_rc", "--noworkspace_rc",
         "--bazelrc="+str(ROOT / "llvm.rc"), "--bazelrc="+str(ROOT / "client.rc")]


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
                "absolute path inclusion",
                "unknown warning option",
                "-Werror",
                "file not found",
                "no such file",
                "static assertion",
                "is required",
                "target is incompatible",
                "Unrecognized option",
                "no such target",
                "No matching toolchain found",
                "The rule cannot be extended",
                "not extendable",
                "parent rule",
                "initializer",
                "must be exported",
                "no such attribute",
                "implicit output",
                "Traceback",
                "Starlark",
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
            names = sorted(case.get("name", "").split("/")[-1].split(".")[-1] for case in cases)
            expected_names = ["PosixError", "PosixErrorOrNullptr", "PosixErrorOrPosixError"]
            if item["label"].startswith("//test/util:"):
                item["namedOriginals"] = [n for n in names if n in expected_names]
                item["allOriginals"] = names == expected_names
            else:
                package = Path("pkg/atomicbitops")
                registered = {n for src in ("aligned_test.go", "atomicbitops_test.go")
                              for n in re.findall(r"(?m)^func (Test\w+)\(t ", (package / src).read_text())}
                observed = {part for c in cases for part in re.split(r"[/\.]", c.get("name", "")) if part in registered}
                item["namedOriginals"] = sorted(observed)
                item["allOriginals"] = len(registered) >= 10 and observed == registered
            item["caseNamesDigest"] = hashlib.sha256("\n".join(sorted(c.get("name", "") for c in cases)).encode()).hexdigest()
            item["nonpassing"] = sum(
                any(c.find(tag) is not None for tag in ("failure", "error", "skipped"))
                or c.get("status") == "notrun"
                for c in cases
            )
        elif name == "test.log":
            match = re.search(rb"(?m)^NATIVE_TEST_WORKER=(x86_64|aarch64)\r?$", raw)
            item["uname"] = match[1].decode() if match else None
            machine = re.search(rb"(?m)^NATIVE_TEST_MACHINE= *(62|183)\r?$", raw)
            item["machine"] = int(machine[1]) if machine else None
            item["elf"] = bool(re.search(rb"(?m)^NATIVE_TEST_IDENT= *7f 45 4c 46\r?$", raw))
            digest = re.search(rb"(?m)^NATIVE_TEST_SHA256=([a-f0-9]{64})\r?$", raw)
            item["binarySHA256"] = digest[1].decode() if digest else None


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
                "--test_output=errors",
                "--extra_toolchains=@llvm//toolchain:all",
                """--run_under=/bin/sh -c 'echo NATIVE_TEST_WORKER=$(/bin/uname -m); echo NATIVE_TEST_IDENT=$(/usr/bin/od -An -N4 -tx1 "$1"); h=$(/usr/bin/sha256sum "$1"); echo NATIVE_TEST_SHA256=${h%% *}; echo NATIVE_TEST_MACHINE=$(/usr/bin/od -An -j18 -N2 -tu2 "$1"); exec "$@"' native-test""",
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
            if "testSummary" in event:
                test_owner = ident.get("testSummary", {}).get("label")
                if test_owner in TESTS:
                    allowed = {"FAILED_TO_BUILD", "PASSED", "FAILED", "NO_STATUS", "INCOMPLETE", "TIMEOUT"}
                    value = event["testSummary"].get("overallStatus")
                    receipt.setdefault("summaries", {})[test_owner] = value if value in allowed else "other"
            identity = ident.get("testResult", {})
            result = event.get("testResult")
            label = identity.get("label")
            if label not in TESTS or not result:
                continue
            info = result.get("executionInfo", {})
            item = {
                "label": label,
                "config": identity.get("configuration", {}).get("id") if re.fullmatch(
                    r"[a-f0-9]{64}", identity.get("configuration", {}).get("id", "")) else None,
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
                    if action.get("mnemonic") == "CppCompile":
                        argv = action.get("commandArgs", [])
                        if argv and Path(argv[0]).name in {"clang", "clang++"}:
                            receipt["declaredClang"] = receipt.get("declaredClang", False) or (
                                ("external" in Path(argv[0]).parts) and any("llvm" in dep for dep in Path(argv[0]).parts[:-1]))
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
                    arch = props.get("Arch")
                    image = props.get("container-image")
                    defaulted = False
                    worker = {
                        "defaulted": defaulted,
                        "physical": physical,
                        "remote": action.get("runner") == "remote",
                        "cached": bool(action.get("cacheHit")),
                        "exit": action.get("exitCode", 0),
                        "emulated": any("qemu-" in x for x in argv),
                        "arch": arch if arch in ("amd64", "arm64") else None,
                        "image": image if image in (UBUNTU, GNU) else None,
                    }
                    receipt["runners"].setdefault(action["targetLabel"], []).append(worker)
        if PHASE == "macro":
            # The untransitioned frontend failed on this arm with 1.0.0.
            # Preserve positive native evidence if 1.0.2 changes that behavior.
            patterns = ("no matching toolchain found for type", "which is required to run tests for target platform",
                        "@bazel_tools//tools/test", "//tools/bazeldefs:linux_amd64", "posix_error_test_arm64")
            found = set()
            paths = [WORK / "variants.log", WORK / "variants.bep"]
            paths += list(Path("bazel-out").glob("*/testlogs/test/util/posix_error_test_arm64/test.log"))
            for path in paths:
                if path.is_file():
                    with path.open(errors="replace") as lines:
                        for line in lines:
                            found.update(word for word in patterns if word.lower() in line.lower())
            receipt["macroFailureEvidence"] = sorted(found)
            receipt["originalFailureReproduced"] = (
                receipt["exit"] != 0 and {"no matching toolchain found for type",
                  "which is required to run tests for target platform", "//tools/bazeldefs:linux_amd64"} <= found
                and not any(w["physical"] and w["exit"] == 0 for w in
                    receipt["runners"].get("//test/util:posix_error_test_arm64", [])))
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
                and attempt.get("allOriginals") and attempt.get("elf") and attempt.get("binarySHA256")
                and attempt.get("config") and attempt.get("machine") == {"amd64": 62, "arm64": 183}[arch]
                and attempt.get("uname") == {"arm64": "aarch64", "amd64": "x86_64"}[arch]
            ):
                return False
        receipt["verified"] = (
            receipt["exit"] == 0
            and set(receipt["owners"]) == set(TESTS)
            and len({v[0]["binarySHA256"] for v in receipt["owners"].values()}) == len(TESTS)
        )
        return receipt["verified"]
    finally:
        write(name, receipt)


if __name__ == "__main__":
    raise SystemExit(0 if tests() else 1)
