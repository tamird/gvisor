"""Recover the original built bytes, then run only the two missing cases."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
import zipfile

from cas_inputs import blob, fetch, rpc
from consumer import BACKTRACE, IMAGE, PRODUCT, RUNSC, RUNTIME, SENTRY, TEST, devices, elf, test_case
from qualify import OUT, TMP, now, save

ORIGINAL = "ac716cf3-de03-44be-97c0-05927b5ca766"
SOURCE = "f917114d928c7b73b3899fdd4492d2120029cf62"
ARCHIVE = "2b9b8ab8ea5c58fcd2b16863ccd78efc78c104843a31da3e1fe72329629aeac7"
LINKS = {
    RUNSC: "ba79427ff0fcd0f7c20041dd6e81a61cd012c9c2236ab3b9a7c4a5d4d422312a",
    SENTRY: "1179972531ee4885f282e8d36c0dd24e04305d5b2146bb2f9e1a2f73d28fd6c2",
    BACKTRACE: "677f10d6f9fcd747cf391e2a2f2a5158bb0268ecf6abee1aafbbd8c7244efc55",
    TEST: "b185831eb5e3ac1acc5f36d4c9c0f254f1ce6fd51e2bd6fd93455f557870d048",
}


def error(exc):
    return {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}


def write_original(digest, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob(digest, ORIGINAL))
    path.chmod(0o755)
    return elf(path)


def link_output(r, owner, path):
    link, = [a for a in r["actions"] if a["mnemonic"] == "GoLink" and a["digest"]["hash"] == LINKS[owner]]
    assert link["targetLabel"] in (owner, owner + "_/" + owner.rsplit(":", 1)[1])
    assert not link["exitCode"] and link["runner"] == "remote" and not link["cacheHit"]
    rows = rpc("GetExecution", {"executionLookup": {"invocationId": ORIGINAL, "actionDigestHash": LINKS[owner]},
                                "inlineExecuteResponse": True}).get("execution", [])
    execution, = [e for e in rows if e.get("actionDigest", {}).get("hash") == LINKS[owner]
                  and str(e["actionDigest"]["sizeBytes"]) == str(link["digest"]["sizeBytes"])]
    assert execution["stage"] in (4, "COMPLETED")
    response = execution["executeResponse"]
    assert not response.get("status", {}).get("code") and response["result"].get("exitCode", 0) == 0
    assert not response.get("cachedResult")
    name = owner.rsplit(":", 1)[1]
    output, = [f for f in response["result"].get("outputFiles", []) if Path(f["path"]).name == name]
    actual = write_original(output["digest"], path)
    if owner in (RUNSC, SENTRY):
        assert actual == r["binaries"][owner]
        r["binaries"][owner]["link"] = link
    return {"action": link["digest"], "output": output["path"], "file": actual, "fresh_original_link": True}


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def sidecars(r, install):
    # The same GetTarget/GetAction read path recovered the prior original
    # plugin release. Only these actual release filenames are materialized.
    names = {"checkpointgofer", "runsc-metric-server", "runsc-fd-parking", "gvisor-sentry-prewarmer",
             "gvisor-proc-umounter", "gvisor_sentry"}
    found = {name: set() for name in names}
    def record(reply):
        for row in walk(reply):
            name = Path(row.get("name") or row.get("path") or "").name
            if name not in names:
                continue
            digest = row.get("digest", row.get("hash", ""))
            size = row.get("length", row.get("sizeBytes", 0))
            if isinstance(digest, dict):
                size, digest = digest.get("sizeBytes"), digest.get("hash")
            match = re.fullmatch(r"bytestream://[^/]+/.*?blobs/([0-9a-f]{64})/(\d+)", row.get("uri", ""))
            if match:
                digest, size = match.groups()
            if isinstance(digest, str) and re.fullmatch("[0-9a-f]{64}", digest) and size:
                found[name].add((digest, int(size)))
    record(rpc("GetTarget", {"invocationId": ORIGINAL, "targetLabel": "//:release"}))
    if any(not choices for choices in found.values()):
        record(json.loads(fetch("api/v1/GetAction", {"selector": {"invocation_id": ORIGINAL, "target_label": "//:release"}})))
    result = r["recovery"]["sidecars"] = {}
    r["recovery"]["sidecar_output_counts"] = {name: len(choices) for name, choices in found.items()}
    save(r)
    for name in sorted(names):
        r["recovery"]["recovering_sidecar"] = name
        save(r)
        digest, = found[name]
        result[name] = write_original({"hash": digest[0], "sizeBytes": digest[1]}, install / "gvisor-bin" / name)
        save(r)
    del r["recovery"]["recovering_sidecar"]
    return result


def recover():
    data = (TMP / "original.zip").read_bytes()
    assert hashlib.sha256(data).hexdigest() == ARCHIVE
    with zipfile.ZipFile(TMP / "original.zip") as z:
        assert set(z.namelist()) == {"result.json", "image.json"}
        r = json.loads(z.read("result.json"))
        (OUT / "image.json").write_bytes(z.read("image.json"))
    assert r["source"] == SOURCE and r["product"] == PRODUCT and r["invocation"] == ORIGINAL
    assert r["status"] == "INCOMPLETE" and r["build_exit"] == 0 and r["build_status"] == "PASS"
    assert r["tests_executed"] == 0 and "socket" not in r and "backtrace" not in r
    r["original_collection"] = {"run": 41369923086, "artifact": 13108819013, "sha256": ARCHIVE,
                                "status": r["status"], "tests_executed": 0, "error": r.pop("collection_error"),
                                "verdict_time": r["verdict_time"], "consumer_inputs": r.pop("consumer_inputs")}
    r["recovery"] = {"carrier": os.environ["GITHUB_SHA"], "started": now(), "status": "INCOMPLETE", "outputs": {}}
    save(r)
    try:
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == os.environ["GITHUB_SHA"]
        assert subprocess.run(["git", "diff", "--quiet", PRODUCT, "HEAD", "--", ".", ":(exclude)test/rbe", ":(exclude).github/workflows/build.yml"]).returncode == 0
        install = TMP / "plugin-installed"
        pkg = Path("test/rbe/plugin_programs/reused")
        destinations = {RUNSC: install / "runsc", SENTRY: install / "gvisor-bin/gvisor_sentry_plugin_stack",
                        BACKTRACE: pkg / "exact_backtrace", TEST: pkg / "exact_socket"}
        for owner, path in destinations.items():
            r["recovery"]["recovering_owner"] = owner
            save(r)
            r["recovery"]["outputs"][owner] = link_output(r, owner, path)
            save(r)
        del r["recovery"]["recovering_owner"]
        sidecars(r, install)
        (pkg / "BUILD").write_text('load("@bazel_skylib//rules:native_binary.bzl", "native_test")\n\n'
                                  'native_test(name = "backtrace", src = "exact_backtrace", size = "large", local = True)\n'
                                  'native_test(name = "socket", src = "exact_socket", size = "large", local = True)\n')
        r["recovery"].update(status="PASS", finished=now(), product_rebuilds=0)
    except Exception as exc:
        r["recovery"].update(error=error(exc), finished=now())
    save(r)
    return r["recovery"]["status"] == "PASS"


def tests():
    r = json.loads((OUT / "result.json").read_text())
    assert r["recovery"]["status"] == "PASS"
    bazel = ["bazel", "--bazelrc=" + str(TMP / "sidecar.bazelrc")]
    flags = ["--config=x86_64", "-c", "opt", "--remote_accept_cached", "--noremote_upload_local_results",
             "--spawn_strategy=remote", "--noremote_local_fallback", "--remote_download_outputs=toplevel",
             "--test_env=GO_TEST_WRAP=1", "--color=no", "--curses=no",
             "--registry=" + (Path(os.environ["GITHUB_WORKSPACE"]) / "registry").as_uri(), "--registry=https://bcr.bazel.build"]
    test_case(r, "backtrace", bazel, flags, "//test/rbe/plugin_programs/reused:backtrace", "TestDumpStack")
    try:
        r["devices"] = devices()
        save(r)
        assert all(item["opened"] for item in r["devices"].values())
        install = TMP / "plugin-installed"
        private = TMP / "plugin-runtime-logs"
        private.mkdir(exist_ok=True)
        subprocess.run(["docker", "pull", IMAGE], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "tag", IMAGE, "gvisor.dev/images/basic/ubuntu"], check=True)
        subprocess.run(["docker", "run", "--rm", "--entrypoint", "/bin/sh", IMAGE, "-c",
                        "set -e; command -v nc; command -v awk; command -v grep"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["sudo", install / "runsc", "install", "--runtime=" + RUNTIME, "--download-sidecars=NEVER", "--",
                        "--network=plugin", "--platform=systrap", "--debug", "--debug-log=" + str(private) + "/"],
                       check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["sudo", "systemctl", "reload", "docker"], check=True)
        entry = json.loads(subprocess.check_output(["sudo", "cat", "/etc/docker/daemon.json"]))["runtimes"][RUNTIME]
        assert entry["path"] == str(install / "runsc")
        assert "--network=plugin" in entry["runtimeArgs"] and "--platform=systrap" in entry["runtimeArgs"]
        r["runtime"] = {"name": RUNTIME, "network": "plugin", "image": IMAGE,
                        "runsc_sha256": elf(install / "runsc")["sha256"],
                        "sentry_sha256": elf(install / "gvisor-bin/gvisor_sentry_plugin_stack")["sha256"]}
        assert r["runtime"]["runsc_sha256"] == r["binaries"][RUNSC]["sha256"]
        assert r["runtime"]["sentry_sha256"] == r["binaries"][SENTRY]["sha256"]
        save(r)
        test_case(r, "socket", bazel, flags, "//test/rbe/plugin_programs/reused:socket", "TestConnectToSelf",
                  ["--test_arg=-runtime=" + RUNTIME])
        # Only booleans from private logs leave the runner. Source selects this
        # sidecar and reports initialization on the actual plugin path.
        marker = 'Sidecar "gvisor_sentry_plugin_stack" found: booting sandbox with ' + str(install / "gvisor-bin/gvisor_sentry_plugin_stack")
        observed = {"plugin_sentry": False, "plugin_initialization": False}
        for path in private.iterdir():
            text = subprocess.check_output(["sudo", "tail", "-c", "1000000", path]).decode(errors="replace")
            observed["plugin_sentry"] |= marker in text
            observed["plugin_initialization"] |= "Initializing plugin network stack, config:" in text
        r["runtime"]["observed"] = observed
        r["vhost_traffic"] = "device opened; TestConnectToSelf uses the same container's local IP, not external vhost traffic"
        if all(r.get(k, {}).get("status") == "PASS" for k in ("backtrace", "socket")) and all(observed.values()):
            r["status"] = "PASS"
    except Exception as exc:
        r["runtime_error"] = error(exc)
    r["verdict_time"] = now()
    save(r)
    return r["status"] == "PASS"


if __name__ == "__main__":
    raise SystemExit(0 if (recover() if sys.argv[1] == "recover" else tests()) else 1)
