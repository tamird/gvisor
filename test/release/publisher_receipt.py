"""Export only typed summaries of the two fixed remote publication actions."""
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile


TESTS = {"publisher_namespace_test": "namespace.json", "publisher_local_test": "publisher.json"}
HEX64 = re.compile("[a-f0-9]{64}")
DIAGNOSTICS = ("Failed RTM_NEWADDR: Operation not permitted", "Permission denied",
               "Operation not permitted", "Setting up uid map", "No permissions to create",
               "No such file or directory", "Failed to mount", "GLIBC_2.38",
               "read-only file system", "Read-only file system", "Failed to extract",
               "FAILED TO BUILD", "UNAVAILABLE", "DEADLINE_EXCEEDED", "RESOURCE_EXHAUSTED",
               "manifest unknown", "Error fetching repository", "failed to download",
               "a root host process", "the local publisher must be exercised without root")


def receive(work, target, code):
    events = work / (target + ".bep")
    result = {"target": target, "bazel_exit": code, "test_results": [], "errors": []}
    seen = set()
    def observe(text):
        for msg in DIAGNOSTICS:
            if msg.lower() in text.lower():
                seen.add(msg)
    def project():
        result["errors"] = sorted(seen)
        (work / "public" / (target + ".json")).write_text(json.dumps(result, indent=2) + "\n")
    try:
        if events.exists():
            for line in events.open():
                event = json.loads(line)
                if "started" in event:
                    uid = event["started"]["uuid"]
                    if re.fullmatch("[0-9a-f-]{36}", uid):
                        result["invocation"] = uid
                if "action" in event and not event["action"].get("success"):
                    owner = event.get("id", {}).get("actionCompleted", {}).get("label", "")
                    if re.fullmatch(r"[@a-zA-Z0-9_./:+-]{1,170}", owner):
                        result.setdefault("failed_owners", []).append(owner)
                if "testResult" in event:
                    if event["id"]["testResult"]["label"] != "//test/release:" + target:
                        raise ValueError("unexpected TestRunner label")
                    test = event["testResult"]
                    execution = test.get("executionInfo", {})
                    row = {"status": test["status"], "strategy": execution.get("strategy"),
                           "local_cached": test.get("cachedLocally", False),
                           "remote_cached": execution.get("cachedRemotely", False)}
                    row["outputs"] = []
                    for output in test.get("testActionOutput", []):
                        match = re.search(r"/blobs/([0-9a-f]{64})/([0-9]+)$", output.get("uri", ""))
                        if match and output["name"] in {"test.log", "test.xml", "test.outputs/outputs.zip"}:
                            row["outputs"].append([output["name"], match[1], int(match[2])])
                    result["test_results"].append(row)
        logdir = Path("bazel-testlogs/test/release") / target
        for path in (work / (target + ".console"), logdir / "test.log"):
            if path.is_file():
                data = path.read_bytes()
                observe(data.decode(errors="replace"))
                if path.name == "test.log":
                    result["log_size"] = len(data)
                    result["log_sha256"] = hashlib.sha256(data).hexdigest()
        name = TESTS[target]
        value = logdir / "test.outputs" / name
        archive = logdir / "test.outputs/outputs.zip"
        raw = None
        if value.is_file() and value.stat().st_size < 32000:
            raw = value.read_bytes()
        elif archive.is_file():
            with zipfile.ZipFile(archive) as z:
                if name in z.namelist() and z.getinfo(name).file_size < 32000:
                    raw = z.read(name)
        if raw:
            actual = json.loads(raw)
            if name == "namespace.json":
                for key in ("uid", "child_exit"):
                    value = actual[key]
                    if type(value) is not int or not 0 <= value <= 2**32:
                        raise ValueError(key)
                    result[key] = value
                observe(actual["diagnostic"])
                for key in ("kernel/apparmor_restrict_unprivileged_userns", "kernel/unprivileged_userns_clone",
                            "user/max_user_namespaces"):
                    if key in actual and re.fullmatch("[0-9]{1,15}", actual[key]):
                        result[key] = int(actual[key])
            else:
                packages = actual["packages"]
                if set(packages) == {"amd64", "arm64"}:
                    for pkg in packages.values():
                        if not HEX64.fullmatch(pkg["sha256"]) or not 1024 < pkg["size"] < 256 << 20:
                            raise ValueError("package fields")
                    result["packages"] = {arch: {"size": pkg["size"], "sha256": pkg["sha256"]} for arch, pkg in packages.items()}
                suites = {}
                for suite in ("release", "20261102"):
                    if suite not in actual["suites"]:
                        continue
                    entry = actual["suites"][suite]
                    hashes = entry["indexes"]
                    if set(hashes) != {f"main/binary-{arch}/Packages{ext}" for arch in ("amd64","arm64") for ext in ("",".gz",".xz")}:
                        raise ValueError("index paths")
                    if not all(HEX64.fullmatch(h) for h in hashes.values()):
                        raise ValueError("index digests")
                    records = entry["independent_keyring"]
                    if len(records) != 2 or not all(re.fullmatch(r"GOODSIG _gpgbuilder [A-F0-9]{40} [0-9]{10,11}", r) for r in records):
                        raise ValueError("signature record")
                    suites[suite] = {"indexes": hashes, "verified_package_signatures": 2}
                result["suites"] = suites
        rows = result["test_results"]
        valid = len(rows) == 1 and rows[0]["status"] == "PASSED" and rows[0]["strategy"] == "remote" and not rows[0]["local_cached"] and not rows[0]["remote_cached"]
        if name == "namespace.json":
            valid = valid and result.get("uid", 0) > 0 and result.get("child_exit") == 0
        else:
            valid = valid and len(result.get("suites", {})) == 2 and len(result.get("packages", {})) == 2
        result["verified"] = bool(not code and valid)
    finally:
        project()
    return result["verified"]


if __name__ == "__main__":
    _, path, target, rc = sys.argv
    if target not in TESTS:
        raise SystemExit("unknown target")
    raise SystemExit(0 if receive(Path(path), target, int(rc)) else 1)
