#!/usr/bin/env python3
"""Observe the real Kind script's Make, extraction, and node-copy arguments."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
import uuid

import full

TMP = Path(os.environ["RUNNER_TEMP"])
TOOL = TMP / "tools"
TRACE = TMP / "witness"
OWNER = "//debian:gvisor-release-tar-bz2"
NODE = "gvisor-e2e-control-plane"


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(name, data):
    (TRACE / name).write_text(json.dumps(data))


def load(name):
    return json.loads((TRACE / name).read_text())


def original():
    prior = json.loads((TMP / "original/amd64.json").read_text())
    assert prior["status"] == "PASS" and prior["source"] == os.environ["PRODUCER"]
    p = prior["packages"]["gvisor.tar.bz2"]
    assert p["verified"] and len(p["members"]) == 9
    return p


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def standin(name, args):
    phase = os.environ["KIND_PHASE"]
    if name == "bazel":
        command = args[0]
        assert command in ("build", "cquery") and not (TRACE / (phase + command)).exists()
        ix = args.index("-c")
        assert args[ix + 1] == "opt" and "--" in args
        assert args[args.index("--")+1:] == ([OWNER] if command == "build" else ['set() union "' + OWNER + '"'])
        assert all("gvisor-release-tar-zstd" not in x for x in args)
        if command == "build":
            assert "--remote_download_outputs=toplevel" in args
        else:
            assert "--starlark:file=tools/show_paths.bzl" in args
        status = 39
        if phase == "positive":
            status = subprocess.run([TOOL / "real-bazel", "--bazelrc=" + str(TMP / "kind.bazelrc"),
                                     command, "--build_event_json_file=" + str(TMP / (command + ".bep")),
                                     *args[1:]]).returncode
        save(phase + command, {"command": command, "owner": OWNER, "opt": True, "exit": status})
        return status
    if phase == "positive":
        if name == "docker" and args == ["ps", "--filter", "name=" + NODE, "--filter", "status=running", "--format", "{{.Names}}"]:
            print(NODE)
            return 0
        if name == "kind" and args[:5] == ["export", "kubeconfig", "--name", "gvisor-e2e", "--kubeconfig"]:
            p = Path(args[5])
            assert args[6:] == (["--internal"] if p.name == "kubeconfig_internal" else [])
            assert p.name in ("kubeconfig", "kubeconfig_internal") and p.parent.parent == TMP / "bin-dirs"
            p.write_text("server: " + NODE + "\n")
            return 0
        if name == "docker" and args == ["inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", NODE]:
            print("192.0.2.1")
            return 0
        if name == "docker" and len(args) == 3 and args[0] == "cp" and args[2] == NODE + ":/usr/bin/":
            files = Path(args[1])
            assert args[1].endswith("/unpacked/.") and files.parent.parent.parent == TMP / "bin-dirs"
            actual = sorted(str(p.relative_to(files)) for p in files.rglob("*") if p.is_file())
            p = original()
            assert actual == sorted(p["members"]) and all(not (files / n).is_symlink() and os.access(files / n, os.X_OK) for n in actual)
            archive = files.parent / "gvisor.tar.bz2"
            assert list(files.parent.glob("*.tar.*")) == [archive]
            actual_sha = sha(archive)
            save("copy", {"source": "unpacked/.", "destination": args[2], "archive_sha256": actual_sha,
                          "archive_bytes": archive.stat().st_size, "prior_archive_identical": actual_sha == p["sha256"],
                          "selected_bazel_output_identical": actual_sha == sha(Path("bazel-bin/debian/gvisor.tar.bz2")),
                          "executable_paths": actual, "at": now()})
            return 73  # Intentional stop at submission to the simulated node.
    save("unexpected", {"tool": name, "phase": phase})
    return 79


def event(command):
    start = end = label = None
    with (TMP / (command + ".bep")).open() as rows:
        for line in rows:
            v = json.loads(line)
            if "started" in v:
                start = v["started"]
            if "finished" in v:
                end = v["finished"]
            if v.get("id", {}).get("targetCompleted", {}).get("label") == OWNER:
                label = v["completed"].get("success", False)
    assert start["buildToolVersion"] == "9.3.1" and start["command"] == command
    assert end["exitCode"].get("code", 0) == 0
    if command == "build":
        assert label is True
    return {"invocation": str(uuid.UUID(start["uuid"])), "bazel": start["buildToolVersion"],
            "finished": end.get("finishTime") or end.get("finishTimeMillis"), "selected_owner_built": label}


def main():
    result = {"product": os.environ["PRODUCT"], "producer": os.environ["PRODUCER"], "prior_package_artifact": 13299000441,
              "scope": "actual script and native Make; local node-copy recorder, no cluster",
              "status": "INCOMPLETE", "arms": {}}
    output = TMP / "result.json"
    output.write_text(json.dumps(result))
    try:
        (TMP / "bin-dirs").mkdir()
        env = os.environ | {"TMPDIR": str(TMP / "bin-dirs"), "MAKEFLAGS": " -- DOCKER_BUILD=false"}
        for phase in ("negative", "positive"):
            arm = result["arms"][phase] = {}
            log = TMP / (phase + ".log")
            with log.open("w") as f:
                arm["script_exit"] = subprocess.run(["timeout", "35m", "bash", TMP / "run_kind_e2e.sh",
                                                      "//test/kubernetes/tests:hello_test"],
                                                     env=env | {"KIND_PHASE": phase}, stdout=f, stderr=subprocess.STDOUT).returncode
            arm["returned_at"] = now()
            for cmd in ("build", "cquery"):
                if (TRACE / (phase + cmd)).is_file():
                    arm[cmd] = load(phase + cmd)
            if (TRACE / "unexpected").is_file():
                arm["unexpected"] = load("unexpected")
            if (TRACE / "copy").is_file():
                arm["copy"] = load("copy")
            output.write_text(json.dumps(result))
            assert not arm.get("unexpected")
            if phase == "negative":
                assert arm["script_exit"] == 2 and arm["build"]["exit"] == 39 and "cquery" not in arm and "copy" not in arm
            else:
                assert arm["script_exit"] == 73 and arm["build"]["exit"] == arm["cquery"]["exit"] == 0
                assert arm["copy"]["prior_archive_identical"] and arm["copy"]["selected_bazel_output_identical"]
                arm["events"] = {cmd: event(cmd) for cmd in ("build", "cquery")}
            arm["verified"] = True
        result["status"] = "PASS"
    except Exception as e:
        result.update(status="FAIL", error=type(e).__name__, line=traceback.extract_tb(e.__traceback__)[-1].lineno)
        if "log" in locals() and log.is_file():
            result["diagnostics"] = full.diagnostics(log)
    finally:
        result["verified_at"] = now()
        output.write_text(json.dumps(result, indent=2))
    return int(result["status"] != "PASS")


if __name__ == "__main__":
    name = Path(sys.argv[0]).name
    sys.exit(main() if name == "witness" else standin(name, sys.argv[1:]))
