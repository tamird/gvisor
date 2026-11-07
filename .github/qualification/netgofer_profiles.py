"""Bind both native presubmit executions to the original action and named test results."""

from collections import Counter
import hashlib
from http.client import HTTPSConnection, HTTPException
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(os.environ["RUNNER_TEMP"]) / "netgofer-native-profiles"
SOURCE = "ac9ce7c6cedddadcba30b2b183ccf7068ab3e551"
OWNERSHIP = "8a120ab6c04ad9b8007be155d16ce0f98b0b87df"
FD = "//pkg/tcpip/link/fdbased:fdbased_test"
BEFORE_CASES = {
    "TestSeqpacketWorkerShutdown": "FAIL",
    "TestSeqpacketWorkerShutdown/PeerEOF": "FAIL",
    "TestSeqpacketWorkerShutdown/DetachAfterEOF": "FAIL",
    "TestSeqpacketWorkerShutdown/AdministrativeStop": "PASS",
}
FULL = "4eb27c4081e535d69c1257998a6c94256d9812e2"
ARCHES = {"before": "x86_64", "amd64": "x86_64", "arm64": "aarch64"}
KNOWN = json.loads((ROOT / "netgofer_profile_cases.json").read_text())
REQUIRED = {
    "//pkg/tcpip/link/fdbased:fdbased_test": {
        "TestSeqpacketWorkerShutdown/PeerEOF", "TestSeqpacketWorkerShutdown/DetachAfterEOF",
        "TestSeqpacketWorkerShutdown/AdministrativeStop", "TestDispatchPacketFormat/recvMMsgDispatcher",
        "TestDetachedReceiveBatch", "TestInjectableAttachment",
    },
}
RECEIPT = ROOT / "receipt.json"


def store(receipt: dict) -> None:
    data = json.dumps(receipt, indent=2)
    assert len(data) < 128 * 1024
    RECEIPT.write_text(data + "\n")


def read_test_file(output: dict, path: Path) -> tuple[bytes, dict]:
    match = re.search(r"/(?:blobs|compressed-blobs/zstd)/([0-9a-f]{64})/(\d+)(?:$|[?])", output.get("uri", ""))
    digest = match[1] if match else output["digest"]
    size = int(match[2] if match else output["length"])
    assert 0 < size <= 4 * 1024 * 1024 and path.stat().st_size == size
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == digest
    return data, {"sha256": digest, "bytes": size}


def test_row(arch: str, label: str, event: dict, row: dict) -> None:
    result = event["testResult"]
    identity = event["id"]["testResult"]
    info = result.get("executionInfo", {})
    row.update(status=result["status"], strategy=info.get("strategy"),
               cached=bool(result.get("cachedLocally") or info.get("cachedRemotely")),
               run=identity.get("run", 1), shard=identity.get("shard", 1), attempt=identity.get("attempt", 1))
    assert row["status"] in {"PASSED", "FAILED", "TIMEOUT", "INCOMPLETE", "FLAKY", "FAILED_TO_BUILD", "REMOTE_FAILURE", "TOOL_HALTED_BEFORE_TESTING"}
    assert row["strategy"] in (None, "remote", "local", "linux-sandbox", "processwrapper-sandbox")
    directory = Path(f"bazel-peer-{arch}-testlogs") / label[2:].replace(":", "/")
    outputs = {o["name"]: o for o in result["testActionOutput"] if o["name"] in ("test.log", "test.xml")}
    log, row["log"] = read_test_file(outputs["test.log"], directory / "test.log")
    xml, row["xml"] = read_test_file(outputs["test.xml"], directory / "test.xml")
    observed = re.findall(rb"(?m)^NATIVE_PEER_WORKER=(x86_64|aarch64)\s*$", log)
    row["native_cpu"] = observed[0].decode() if len(observed) == 1 else None
    names = {}
    for case in ET.fromstring(xml).iter("testcase"):
        top, slash, sub = case.attrib["name"].partition("/")
        name = top.rsplit(".", 1)[-1] + slash + sub
        assert name not in names
        if case.find("skipped") is not None:
            state = "SKIP"
        elif case.find("error") is not None or case.find("failure") is not None:
            state = "FAIL"
        else:
            state = "PASS"
        names[name] = state
    known = set(BEFORE_CASES if arch == "before" else KNOWN[label])
    row["counts"] = dict(Counter(names.values()))
    row["skipped"] = sorted(name for name in known if names.get(name) == "SKIP")
    row["failed"] = sorted(name for name in known if names.get(name) == "FAIL")
    row["missing"] = sorted(known - names.keys())
    row["unknown_count"] = len(names.keys() - known)
    row["named_passes"] = sorted(name for name in REQUIRED[label] if names.get(name) == "PASS" and
                                 ("--- PASS: " + name + " (").encode() in log)
    fresh_native = (row["strategy"] == "remote" and not row["cached"] and row["native_cpu"] == ARCHES[arch] and
                    row["run"] == row["shard"] == row["attempt"] == 1)
    if arch == "before":
        row["early_notification"] = b"ran before the occupied worker returned" in log
        row["final_packet_missing"] = b"delivered packets = [1], want [1 2]" in log
        row["outcomes"] = {k: names.get(k) for k in BEFORE_CASES}
        row["logged"] = sorted(name for name, state in BEFORE_CASES.items()
                               if ("--- " + state + ": " + name + " (").encode() in log)
        row["verified"] = (fresh_native and row["status"] == "FAILED" and names == BEFORE_CASES and
                           len(row["logged"]) == len(BEFORE_CASES) and row["early_notification"] and
                           row["final_packet_missing"])
        return
    row["verified"] = (row["status"] == "PASSED" and fresh_native and
                       not row["missing"] and not row["unknown_count"] and not row["failed"] and
                       set(row["named_passes"]) == REQUIRED[label])


def tests(arch: str, profile: dict) -> None:
    profile["exit"] = int((ROOT / (arch + ".exit")).read_text())
    if profile["exit"] != (3 if arch == "before" else 0):
        profile["compiler"] = []
        with (ROOT / (arch + ".console")).open(errors="replace") as console:
            for line in console:
                loc = re.search(r"pkg/tcpip/link/fdbased/(endpoint_test|endpoint|processors|packet_dispatchers)\.go:(\d+):", line)
                categories = [text for text in ("cannot use", "undefined:", "not enough arguments", "too many arguments")
                              if text in line]
                if loc and categories:
                    profile["compiler"].append([loc[1], int(loc[2]), categories])
                    if len(profile["compiler"]) == 8:
                        break
    rows = profile["tests"] = {}
    with (ROOT / (arch + ".bep")).open() as f:
        for line in f:
            event = json.loads(line)
            if "started" in event:
                profile["invocation"] = str(uuid.UUID(event["started"]["uuid"]))
            if "testResult" in event:
                label = event["id"]["testResult"]["label"]
                assert label in REQUIRED and label not in rows
                row = rows[label] = {"verified": False}
                try:
                    test_row(arch, label, event, row)
                except (OSError, AssertionError, ET.ParseError, ValueError, KeyError) as exc:
                    row["error"] = type(exc).__name__
    labels = {FD} if arch == "before" else REQUIRED.keys()
    profile["tests_verified"] = (profile["exit"] == (3 if arch == "before" else 0) and rows.keys() == labels and
                                 all(r["verified"] for r in rows.values()))


def workers(arch: str, profile: dict) -> None:
    sys.path.insert(0, str(ROOT))
    import exec_cas as cas
    from source_presubmit_workers import platform_fields

    cas.INV = str(uuid.UUID(profile["invocation"]))
    rows = profile["workers"] = {}
    for label in ((FD,) if arch == "before" else REQUIRED):
        matches = rows[label] = []
        try:
            executions = cas.executions(label, "TestRunner")
            assert len(executions) <= 8
            for e in executions:
                rep = e.get("executeResponse", {})
                if "result" not in rep:
                    continue
                out = rep["result"]
                digest = e["actionDigest"]
                action = dict(cas.fields(cas.blob(digest, 128 * 1024)))
                command = list(cas.fields(cas.blob(cas.digest_msg(action[1]), 4 * 1024 * 1024)))
                argv = [v.decode() for tag, v in command if tag == 1]
                if not (any(a.endswith("/test-setup.sh") for a in argv) or
                        any(f.get("path", "").endswith("/test.cache_status") for f in out.get("outputFiles", []))):
                    continue
                platform = platform_fields(action[10]) if 10 in action else next(
                    (platform_fields(v) for tag, v in command if tag == 5), {})
                row = {"platform": platform, "action_sha256": digest["hash"], "exit": out.get("exitCode", 0),
                       "rpc": rep.get("status", {}).get("code", 0), "cached": bool(rep.get("cachedResult")),
                       "qemu": any("qemu-" in a for a in argv)}
                expected_exit = row["exit"] != 0 if arch == "before" else row["exit"] == 0
                cpu = "amd64" if arch == "before" else arch
                row["verified"] = (expected_exit and platform.get("cpu") == cpu and platform.get("class") == "oci" and
                                   platform.get("linux") and platform.get("ubuntu") and
                                   not any(row[k] for k in ("rpc", "cached", "qemu")))
                matches.append(row)
        except (OSError, IndexError, AssertionError, ValueError, KeyError) as exc:
            matches.append({"error": type(exc).__name__, "verified": False})
    profile["workers_verified"] = all(len(found) == 1 and found[0]["verified"] for found in rows.values())



def original_compiler() -> dict:
    """Classify only the original AMD64 compiler error without retaining its console."""
    invocation = "70736dc5-2faa-4f62-87e4-fc442095930a"
    observed = {"invocation": invocation, "recognized": False, "pages": 0}
    digest = hashlib.sha256()
    token = "0000"
    tail = ""
    for _ in range(12):
        client = HTTPSConnection("app.buildbuddy.io", timeout=15)
        try:
            payload = {"selector": {"invocation_id": invocation}, "page_token": token}
            client.request("POST", "/api/v1/GetLog", json.dumps(payload),
                           {"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["ACCESS"]})
            response = client.getresponse()
            assert response.status == 200
            raw = response.read(1024 * 1024 + 1)
            assert len(raw) <= 1024 * 1024
        finally:
            client.close()
        page = json.loads(raw)
        body = page.get("log", {}).get("contents", "")
        digest.update(body.encode())
        observed["pages"] += 1
        token = page.get("nextPageToken") or page.get("next_page_token")
        lines = (tail + body).split("\n")
        tail = lines.pop()
        if not token:
            lines.append(tail)
        if any("endpoint_test.go:638:" in line and "cannot use newRecvMMsgDispatcher" in line for line in lines):
            observed.update(recognized=True, path="pkg/tcpip/link/fdbased/endpoint_test.go", line=638,
                            category="cannot use constructor as function value")
        if not token or observed["recognized"]:
            break
        assert re.fullmatch(r"[a-zA-Z0-9_+=/-]{1,256}", token)
    observed["read_sha256"] = digest.hexdigest()
    return observed


def main() -> None:
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    assert not (ROOT / "client.rc").exists()
    assert set((ROOT / "owner.targets").read_text().splitlines()) == REQUIRED.keys() == KNOWN.keys()
    assert (ROOT / "before.targets").read_text().splitlines() == [FD]
    stage = sys.argv[1]
    if stage == "tests":
        receipt = {"source": SOURCE, "full_presubmit_parent": FULL, "drain": "fbad38ea661236e230a355fde5d0f87d9cac44bb",
                   "before_source": OWNERSHIP, "upstream_ownership": "66eb55f0e3c5a734303e99b6b28baccadba1fb31",
                   "profiles": {cpu: {} for cpu in ARCHES}, "verified": False}
    elif stage == "workers":
        receipt = json.loads(RECEIPT.read_bytes())
        try:
            receipt["original_compiler"] = original_compiler()
        except (AssertionError, KeyError, OSError, ValueError, HTTPException) as exc:
            receipt["original_compiler"] = {"error": type(exc).__name__, "recognized": False}
    else:
        raise ValueError("stage")
    for arch, profile in receipt["profiles"].items():
        try:
            (tests if stage == "tests" else workers)(arch, profile)
        except (OSError, AssertionError, ValueError, KeyError) as exc:
            profile[stage + "_error"] = type(exc).__name__
        store(receipt)
    receipt["verified"] = all(p.get("tests_verified") and p.get("workers_verified")
                              for p in receipt["profiles"].values())
    if stage == "workers":
        receipt["verified"] = bool(receipt["verified"] and receipt["original_compiler"].get("recognized"))
    store(receipt)


if __name__ == "__main__":
    main()
