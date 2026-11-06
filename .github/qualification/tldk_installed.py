"""Exercise the installed GNU plugin binaries across the native Docker bridge."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time
import traceback

import consumer
import native_cross_inputs as evidence

SOURCE = "b317c6e36e644f75da779365ad8eeab2f6db5b69"
PRIOR = {
    "runsc": "7e4421286bcf337fcc96aecfd9f1290c97df923d75eece2f1de797dc50a52433",
    "gvisor-bin/gvisor_sentry_plugin_stack": "b3a4bfdf5da5f928d5e2423cd9127e166370dd724899e8b998ab5fb5bad1ff7a",
}
# Files copied by tools/release.bzl and the plugin sidecar selected by Make.
LAYOUT = sorted(set(PRIOR) | {"containerd-shim-runsc-v1"} | {
    "gvisor-bin/" + name for name in ("checkpointgofer", "gvisor_sentry",
        "runsc-metric-server", "runsc-netgofer", "runsc-fd-parking",
        "gvisor-sentry-prewarmer", "gvisor-proc-umounter")})
FLAGS = ["--config=x86_64", "-c", "opt", "--jobs=4", "--remote_cache=",
         "--remote_executor=", "--disk_cache="]
DIR = Path(os.environ["RUNNER_TEMP"]) / "tldk-llvm"
INSTALL = DIR / "installed"
RUNSC = INSTALL / "runsc"
SENTRY = INSTALL / "gvisor-bin/gvisor_sentry_plugin_stack"
IMAGE = "python@sha256:69ebe56e2a764c399514a0a6117131cb730cc4d0e71e85c08deb9af74cbb9f41"
TEST = "//test/rbe/plugin_programs:external_traffic"
R = {"status": "INCOMPLETE", "source": SOURCE, "carrier": os.environ["CARRIER"],
     "prior_artifact": 13463448452}


def save():
    (DIR / "receipt.json").write_text(json.dumps(R, indent=2) + "\n")


def phase(name):
    R["phase"] = name
    save()


def checked(*args, timeout=120):
    rc, out, err = evidence.command(list(map(str, args)), timeout)
    if rc:
        R["command_failure"] = {"program": Path(args[0]).name, "exit": rc,
                                "diagnostics": evidence.diagnostics(err + b"\n" + out)}
        raise RuntimeError("command failed")
    return out


def prepare():
    assert checked("git", "rev-parse", "HEAD").decode().strip() == SOURCE
    assert checked("bazel", "--version").strip() == b"bazel 9.3.1"
    phase("public Make refresh")
    checked("make", "DOCKER_BUILD=false", "RUNTIME_DIR=" + str(INSTALL),
            "BAZEL_OPTIONS=" + " ".join(FLAGS),
            "RUNSC_TARGET=--config=plugin-tldk " + consumer.RUNSC,
            "EXTRA_SIDECAR_TARGETS=--config=plugin-tldk " + consumer.SENTRY,
            "refresh", timeout=5700)
    R["make_refresh_exit"] = 0
    R["binaries"] = {}
    for rel in LAYOUT:
        path = INSTALL / rel
        assert path.is_file() and not path.is_symlink(), rel
        assert os.access(path, os.X_OK), rel
        info = consumer.elf(path)
        if rel in PRIOR:
            info["equals_prior_installed"] = info["sha256"] == PRIOR[rel]
        R["binaries"][rel] = info
    save()
    # The allowlist contains product ELFs only. Preserve their executable modes
    # and the sidecar directory structure, independently of downstream results.
    archive = DIR / "installed.tar.gz"
    with tarfile.open(archive, "w:gz", compresslevel=1) as bundle:
        for rel in LAYOUT:
            bundle.add(INSTALL / rel, arcname=rel, recursive=False)
    R["recovery_bundle"] = {"members": LAYOUT, "bytes": archive.stat().st_size,
                            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    save()


def runtime():
    R["devices"] = consumer.devices()
    save()
    if not all(row["opened"] for row in R["devices"].values()):
        R["traffic_status"] = "BLOCKED: native device unavailable"
        save()
        return
    phase("public runsc install with sidecar downloads disabled")
    logs = DIR / "logs"
    logs.mkdir()
    checked("sudo", "-n", RUNSC, "install", "--runtime=" + consumer.RUNTIME,
            "--download-sidecars=NEVER", "--", "--network=plugin",
            "--platform=systrap", "--debug", "--debug-log=" + str(logs) + "/")
    R["runsc_install_exit"] = 0
    checked("sudo", "-n", "systemctl", "reload", "docker")
    entry = json.loads(checked("sudo", "-n", "cat", "/etc/docker/daemon.json"))["runtimes"][consumer.RUNTIME]
    assert entry["path"] == str(RUNSC)
    assert {"--network=plugin", "--platform=systrap"} <= set(entry["runtimeArgs"])
    for _ in range(20):
        if consumer.RUNTIME in json.loads(checked("docker", "info", "--format", "{{json .Runtimes}}")):
            break
        time.sleep(1)
    else:
        raise RuntimeError("Docker did not register the plugin runtime")
    R["docker_runtime"] = {"name": consumer.RUNTIME, "path": entry["path"],
                           "args": entry["runtimeArgs"]}
    fixture = "test/rbe/plugin_programs/"
    checked("git", "restore", "--source=" + R["carrier"], "--",
            fixture + "BUILD", fixture + "external_traffic.py")
    checked("docker", "pull", IMAGE, timeout=240)
    image_id = checked("docker", "image", "inspect", "-f", "{{.Id}}", IMAGE).decode().strip()
    phase("real plugin and runc guest exchanges")
    try:
        consumer.test_case(R, "external", ["bazel"], FLAGS, TEST, "TestExternalTraffic",
                           ["--test_timeout=180", "--test_env=REPORT_DIR=" + str(DIR)],
                           owners=("external",))
    finally:
        markers = {"plugin_sentry": 'Sidecar "gvisor_sentry_plugin_stack" found: booting sandbox with ' + str(SENTRY),
                   "plugin_init": "Initializing plugin network stack, config:"}
        R["observed"] = dict.fromkeys(markers, False)
        for path in logs.iterdir():
            rc, content, _ = evidence.command(["sudo", "-n", "tail", "-c", "4000000", str(path)], 30)
            if not rc:
                for key, needle in markers.items():
                    R["observed"][key] |= needle.encode() in content
        wire = DIR / "wire.json"
        if wire.is_file():
            R["wire"] = {}
            for name, row in json.loads(wire.read_text()).items():
                details = {k: row[k] for k in ("runtime", "ready", "ok", "guest_exit", "interface") if k in row}
                details["image_matches"] = row.get("image") == image_id
                details["host"] = {p: {k: v for k, v in data.items() if k in ("sent", "received", "ok", "error")}
                                   for p, data in row.get("host", {}).items()}
                details["bridge"] = {k: {"bytes": v["bytes"], "sha256": v["sha256"]}
                                     for k, v in row.get("wire", {}).items()}
                if err := row.get("error"):
                    details["error"] = {k: err[k] for k in ("type", "errno", "line", "exit") if k in err}
                    if diagnostic := err.get("diagnostic"):
                        details["docker_diagnostic"] = evidence.diagnostics(diagnostic.encode())
                R["wire"][name] = details
        save()
    assert set(R.get("wire", {})) == {"runc", consumer.RUNTIME}
    for name, row in R["wire"].items():
        assert row.get("runtime") == name and row.get("ok") and row["image_matches"], name
        assert row.get("ready") and row.get("guest_exit") == "0" and row.get("interface") == "docker0", name
        assert set(row["bridge"]) == {"tcp_request", "tcp_reply", "udp_request", "udp_reply"}, name
    assert R["external"]["status"] == "PASS" and all(R["observed"].values())
    R["traffic_status"] = "PASS"
    save()


def help_probe():
    rc, out, err = evidence.command([str(SENTRY), "help", "syscalls", "--format=json",
                                     "--os=linux", "--arch=amd64"], 60)
    probe = R["help_syscalls"] = {"exit": rc, "diagnostics": evidence.diagnostics(err)}
    if not rc:
        try:
            probe["read_entry"] = json.loads(out)["linux"]["amd64"]["syscalls"]["0"]["name"]
        except (ValueError, KeyError, TypeError) as exc:
            probe["collection_error"] = type(exc).__name__
    probe["status"] = "PASS" if probe.get("read_entry") == "read" else "FAIL"


try:
    prepare()
    try:
        runtime()
    finally:
        # An optional CLI probe cannot stop guest startup or wire observation.
        help_probe()
    if R.get("traffic_status") == "PASS":
        R["status"] = "PASS"
except Exception as exc:
    R["error"] = {"type": type(exc).__name__, "lines": [f.lineno for f in traceback.extract_tb(exc.__traceback__)]}
finally:
    R["verdict_at"] = consumer.now()
    save()
raise SystemExit(0 if R["status"] == "PASS" else 1)
