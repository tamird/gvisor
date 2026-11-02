"""Recover the original built bytes, then run only the missing VFIO-on socket case."""

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
from consumer import IMAGE, PRODUCT, RUNSC, RUNTIME, SENTRY, TEST, devices, elf, install_runtime, test_case
from qualify import OUT, TMP, now, save

ORIGINAL = "d2c2f184-a6b4-4528-92a0-c2309546d07d"
TEST_LINK_ORIGINAL = "ac716cf3-de03-44be-97c0-05927b5ca766"
SOURCE = "a4a858875a238017693c183b02001c721051f1f4"
ARCHIVE = "9ae503cfa1ad493a54223c313554970d5db303911af9dbcd07df886ae2522cae"
LINKS = {
    RUNSC: "f6cfe50accb4b961cf685500c002cdec6edf554190100ef12095b112b51d24c3",
    SENTRY: "c36e625b09924efeedc9ae42d13e722f115b26b91e3c5654d6b7940c39c141eb",
    TEST: "b185831eb5e3ac1acc5f36d4c9c0f254f1ce6fd51e2bd6fd93455f557870d048",
}


def error(exc):
    return {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}


def write_original(digest, path, invocation=ORIGINAL):
    path.parent.mkdir(parents=True, exist_ok=True)
    assert not path.exists()
    path.write_bytes(blob(digest, invocation))
    path.chmod(0o555)
    # The Go integration test and ordinary sidecars run on populated Ubuntu;
    # only the actual static owners have a no-loader/no-library contract.
    header = subprocess.check_output(["readelf", "-h", path], text=True)
    programs = subprocess.check_output(["readelf", "-l", path], text=True)
    dynamic = subprocess.check_output(["readelf", "-d", path], text=True)
    assert "Advanced Micro Devices X86-64" in header and "ELF64" in header
    interpreters = re.findall(r"\[Requesting program interpreter: ([^\]]+)\]", programs)
    needed = re.findall(r"\(NEEDED\).*Shared library: \[([^\]]+)\]", dynamic)
    assert len(interpreters) <= 1 and bool(interpreters) == ("INTERP" in programs)
    assert len(needed) == dynamic.count("(NEEDED)")
    return {"sha256": digest["hash"], "bytes": int(digest["sizeBytes"]), "machine": "x86_64",
            "interpreter": interpreters[0] if interpreters else None, "needed": needed}


def preflight_roles(r):
    rows = r["recovery"]["outputs"]
    checks = {}
    for owner in LINKS:
        actual = rows[owner]["file"]
        static = owner in (RUNSC, SENTRY)
        checks[owner] = {"native_amd64": actual["machine"] == "x86_64", "requires_static": static,
                         "is_static": actual["interpreter"] is None and not actual["needed"]}
    for name, actual in r["recovery"]["sidecars"].items():
        checks["sidecar:" + name] = {"native_amd64": actual["machine"] == "x86_64", "requires_static": False,
                                   "is_static": actual["interpreter"] is None and not actual["needed"]}
    r["recovery"]["elf_preflight"] = checks
    save(r)
    assert all(c["native_amd64"] and (not c["requires_static"] or c["is_static"]) for c in checks.values())
    # These are the exact prior producer's two static-deliverable observations.
    assert all(rows[o]["file"] == {k: v for k, v in r["binaries"][o].items() if k not in ("link", "output_path")} for o in (RUNSC, SENTRY))


def link_output(r, owner, path):
    link, = [a for a in r["actions"] if a["mnemonic"] == "GoLink" and a["digest"]["hash"] == LINKS[owner]]
    assert link["targetLabel"] in (owner, owner + "_/" + owner.rsplit(":", 1)[1])
    assert not link["exitCode"] and not link["status"]
    # This unchanged test action was reused from the original qualified build;
    # only the two deliverables linked freshly in the VFIO-on invocation.
    if owner == TEST:
        assert link["runner"] == "remote cache hit" and link["cacheHit"]
        invocation = TEST_LINK_ORIGINAL
    else:
        assert link["runner"] == "remote" and not link["cacheHit"]
        invocation = ORIGINAL
    rows = rpc("GetExecution", {"executionLookup": {"invocationId": invocation, "actionDigestHash": LINKS[owner]},
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
        r["binaries"][owner]["link"] = link
    return {"action": link["digest"], "output": output["path"], "file": actual, "link_invocation": invocation, "producer_cache_hit": bool(link["cacheHit"])}


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
    assert r["tests_executed"] == r["passed"] == 1 and r["failed"] == r["skipped"] == 0
    assert r["compiler_preflight"]["status"] == r["consumer_inputs"]["status"] == "PASS"
    assert r["backtrace"]["status"] == "PASS" and r["backtrace"]["cases"] == [
        {"class": "backtrace_test", "name": "TestDumpStack", "status": "PASS"}]
    assert "socket" not in r and r["collection_error"]["type"] == "PermissionError"
    r["original_collection"] = {"run": 41376238963, "artifact": 13111068733, "sha256": ARCHIVE,
                                "status": r["status"], "tests_executed": 1, "error": r.pop("collection_error"),
                                "verdict_time": r["verdict_time"], "backtrace": r["backtrace"]}
    r["recovery"] = {"carrier": os.environ["GITHUB_SHA"], "started": now(), "status": "INCOMPLETE", "outputs": {}}
    r["manual_interventions"] = ["select final runtime sources before preserving read-only output modes"]
    save(r)
    try:
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == os.environ["GITHUB_SHA"]
        assert subprocess.run(["git", "diff", "--quiet", SOURCE, "HEAD", "--", ".", ":(exclude)test/rbe", ":(exclude).github/workflows/build.yml"]).returncode == 0
        staged = TMP / "original-executables"
        install = TMP / "plugin-installed"
        pkg = Path("test/rbe/plugin_programs/reused")
        destinations = {RUNSC: staged / "runsc", SENTRY: staged / "gvisor-bin/gvisor_sentry_plugin_stack",
                        TEST: pkg / "exact_socket"}
        for owner, path in destinations.items():
            r["recovery"]["recovering_owner"] = owner
            save(r)
            r["recovery"]["outputs"][owner] = link_output(r, owner, path)
            save(r)
        del r["recovery"]["recovering_owner"]
        sidecars(r, staged)
        preflight_roles(r)
        release = {name: staged / "gvisor-bin" / name for name in r["recovery"]["sidecars"]}
        # Exercise the duplicate runsc slot with the actual read-only binary:
        # it must be copied once even when also present in the release map.
        release["runsc"] = destinations[RUNSC]
        sources = install_runtime(install, release, destinations[RUNSC], destinations[SENTRY])
        modes = {}
        for name, source in sources.items():
            target = install / (name if name == "runsc" else "gvisor-bin/" + name)
            assert source.stat().st_mode & 0o777 == target.stat().st_mode & 0o777 == 0o555
            assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(target.read_bytes()).digest()
            modes[name] = "0555"
        r["recovery"]["readonly_install"] = {"status": "PASS", "modes": modes, "copied_once_per_final_name": True}
        (pkg / "BUILD").write_text('load("@bazel_skylib//rules:native_binary.bzl", "native_test")\n\n'
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
    r["followup_tests_executed"] = sum(c["status"] != "SKIP" for c in r.get("socket", {}).get("cases", []))
    r["verdict_time"] = now()
    save(r)
    return r["status"] == "PASS"


if __name__ == "__main__":
    raise SystemExit(0 if (recover() if sys.argv[1] == "recover" else tests()) else 1)
