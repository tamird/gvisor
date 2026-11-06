"""Read two original race outputs. Perform native inspection in a separate step."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib import error, request

from llvm_coverage_four import inspect_elf


INV = "17d83e4a-eb73-45c8-a322-8e5c69af2149"
BASE = "https://app.buildbuddy.io/"
WORK = Path(os.environ["RUNNER_TEMP"]) / "llvm-race-original"
TARGETS = {
    "//runsc:runsc_race_coverage": "runsc_race_cov",
    "//runsc/cmd/sentry:gvisor_sentry_race_coverage": "gvisor_sentry_race_cov",
}
SHA = re.compile(r"[0-9a-f]{64}")
LIMIT = 185 * 1024 * 1024
ROUTES = {"rpc/BuildBuddyService/GetTarget", "rpc/BuildBuddyService/GetExecution", "api/v1/GetFile"}


class Fixed(request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def fetch(route, body, limit, key):
    assert route in ROUTES
    req = request.Request(BASE + route, data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", "x-buildbuddy-api-key": key})
    with request.build_opener(Fixed).open(req, timeout=120) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("response bound")
    return data


def signature(f):
    digest = f.get("digest", "")
    size = f.get("length", 0)
    if isinstance(digest, dict):
        size = digest.get("sizeBytes", digest.get("size_bytes", 0))
        digest = digest.get("hash", "")
    if not digest:
        match = re.fullmatch(r"bytestream://(?:remote|cache)\.buildbuddy\.io/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/(\d+)", f.get("uri", ""))
        if match:
            digest, size = match.groups()
    if isinstance(digest, str) and SHA.fullmatch(digest) and str(size).isdigit() and 1024 < int(size) <= LIMIT:
        return digest, int(size)
    return None


def names(row, field, files):
    picked = set()
    for f in files:
        name = Path(f.get("name", f.get("path", ""))).name
        if not re.fullmatch(r"[a-zA-Z0-9_.+\-]{1,100}", name):
            continue
        if len(row[field]) < 12 and name not in row[field]:
            row[field].append(name)
        if name not in row["accepted_names"]:
            continue
        signed = signature(f)
        if signed:
            picked.add(signed)
    return picked


def target(row, label, key):
    route = "rpc/BuildBuddyService/GetTarget"
    result = json.loads(fetch(route, {"invocationId": INV, "targetLabel": label, "status": 0}, 1024*1024, key))
    selected = set()
    for group in result.get("targetGroups", result.get("target_groups", [])):
        for t in group.get("targets", []):
            if t.get("metadata", {}).get("label") in (label, "@@" + label):
                selected.update(names(row, "target_files", t.get("files", [])))
    return selected


def links(row, label, key):
    pkg, name = label.split(":")
    # with_cfg constructs NAME_/NAME when using a macro. The gVisor wrapper
    # constructs NAME_impl around the actual statically linked go_binary.
    original = pkg + ":" + name + "_/" + name + "_impl"
    for candidate in (original, label + "_impl", label):
        for owner in (candidate, "@@" + candidate):
            r = json.loads(fetch("rpc/BuildBuddyService/GetExecution",
                {"executionLookup": {"invocationId": INV, "targetLabel": owner},
                 "inlineExecuteResponse": True}, 5*1024*1024, key))
            found = set()
            for e in r.get("execution", []):
                if e.get("targetLabel") not in (candidate, "@@" + candidate) or e.get("actionMnemonic") != "GoLink":
                    continue
                reply = e.get("executeResponse", {})
                action = reply.get("result")
                if action is None or action.get("exitCode", 0) or reply.get("status", {}).get("code", 0):
                    continue
                found.update(names(row, "link_files", action.get("outputFiles", [])))
            if found:
                row["link_owner"] = candidate
                return found
    return set()


def reason(exc):
    return "HTTP " + str(exc.code) if isinstance(exc, error.HTTPError) else type(exc).__name__


def recover(key):
    data = {"invocation": INV, "source": "c0511e1cd383b0f2870c15a104222f0d285aac6f", "targets": {}}
    WORK.mkdir(exist_ok=True)
    for label, underlying in TARGETS.items():
        row = {"accepted_names": [underlying, label.split(":")[1]], "target_files": [], "link_files": []}
        data["targets"][label] = row
        try:
            originals = target(row, label, key)
        except (OSError, ValueError) as exc:
            row["target_error"] = reason(exc)
            originals = set()
        if len(originals) == 1:
            row["evidence"] = "original target files"
        elif not originals:
            try:
                originals = links(row, label, key)
                row["evidence"] = "original GoLink output via source-declared aliases"
            except (OSError, ValueError) as exc:
                row["action_error"] = reason(exc)
        row["digest_count"] = len(originals)
        if len(originals) == 1:
            digest, size = next(iter(originals))
            row.update(expected_sha256=digest, size=size)
            try:
                uri = "bytestream://remote.buildbuddy.io/blobs/" + digest + "/" + str(size)
                blob = fetch("api/v1/GetFile", {"uri": uri}, size, key)
                if len(blob) == size and hashlib.sha256(blob).hexdigest() == digest:
                    path = WORK / underlying
                    path.write_bytes(blob)
                    path.chmod(0o500)
                    row["download_verified"] = True
            except (ValueError, OSError) as exc:
                row["file_error"] = reason(exc)
        report = json.dumps(data, indent=2).encode()
        assert key.encode() not in report and len(report) < 12000
        (WORK / "result.json").write_bytes(report)


def inspect():
    assert os.environ.get("BUILDBUDDY_API_KEY") is None
    path = WORK / "result.json"
    result = json.loads(path.read_text())
    for label, basename in TARGETS.items():
        row = result["targets"].get(label)
        if not row or not row.get("download_verified"):
            continue
        binary = WORK / basename
        try:
            row.update(inspect_elf(binary))
            row["verified"] = bool(row.get("sha256") == row["expected_sha256"] and
                                   row.get("machine") == 62 and row.get("exec_load") and
                                   row.get("nm_exit") == 0 and row.get("go_cover_symbols", 0) > 0
                                   and row.get("race_runtime_symbols", 0) > 0)
            if label.startswith("//runsc:") and row["verified"]:
                cli = subprocess.run([str(binary), "--version"], capture_output=True, timeout=30)
                row["native_cli_exit"] = cli.returncode
                row["native_cli_version"] = cli.stdout.startswith(b"runsc version ")
                row["verified"] = row["native_cli_exit"] == 0 and row["native_cli_version"]
        finally:
            path.write_text(json.dumps(result, indent=2))
            binary.unlink(missing_ok=True)
    result["verified"] = all(result["targets"].get(label, {}).get("verified") for label in TARGETS)
    path.write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    if sys.argv[1] == "recover":
        recover(os.environ.pop("BUILDBUDDY_API_KEY"))
    elif sys.argv[1] == "inspect":
        inspect()
    else:
        raise SystemExit(2)
