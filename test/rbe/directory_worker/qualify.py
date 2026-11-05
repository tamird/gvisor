# Copyright 2026 The gVisor Authors.
# SPDX-License-Identifier: Apache-2.0
"""Run the directory-image check from a trusted Linux Bazel coordinator."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import uuid

import verify

PACKAGE = "//test/rbe/directory_worker:"
CONFIGS = {"amd64": "x86_64", "arm64": "aarch64"}


def call(args, *, text=False, log=None):
    return subprocess.run(args, stdout=subprocess.PIPE if log is None else log,
                          stderr=subprocess.STDOUT, text=text, timeout=900)


def bazel_path(command, *query):
    proc = call([*command, *query], text=True)
    if proc.returncode:
        raise RuntimeError("Bazel output lookup")
    return Path(proc.stdout.strip().splitlines()[-1])


def run(args):
    local = [str(args.bazel)]
    no_remote = ["--remote_executor=", "--remote_cache=", "--bes_backend=", "--spawn_strategy=local"]
    folder = args.work.resolve()
    folder.mkdir(exist_ok=True)
    result = {"image": {}, "architectures": {}}

    def save():
        (folder / "receipt.json").write_text(json.dumps(result, sort_keys=True) + "\n")

    save()
    with (folder / "image.log").open("wb") as log:
        rc = call([*local, "build", *no_remote, PACKAGE+"layout"], log=log).returncode
    result["image"]["build_exit"] = rc
    save()
    if rc:
        return False
    layout = bazel_path(local, "cquery", *no_remote, "--output=files", PACKAGE+"layout")
    require_layout = layout.parts and layout.parts[0] == "bazel-out" and ".." not in layout.parts
    verify.require(require_layout, "declared layout path")
    layout = bazel_path(local, "info", "execution_root") / layout
    images = verify.inventory(layout)
    result["image"]["inventory"] = images
    save()
    for arch, config in CONFIGS.items():
        row = result["architectures"][arch] = {"status": "INCOMPLETE", "invocation": str(uuid.uuid4())}
        save()
        try:
            flag = "--config=" + config
            with (folder / (arch+".local.log")).open("wb") as log:
                row["observer_build_exit"] = call([*local, "build", *no_remote, flag, PACKAGE+"observer"], log=log).returncode
            save()
            if row["observer_build_exit"]:
                continue
            run = [str(args.bazel), "--bazelrc="+str(args.rc)]
            image = "docker://"+args.repository+"@"+images[arch]["manifest"]
            properties = {"Arch": arch, "OSFamily": "linux", "container-image": image,
                          "workload-isolation-type": "oci", "recycle-runner": "false"}
            bep = folder / (arch+".bep")
            spawns = folder / (arch+".spawns")
            logfile = folder / (arch+".remote.log")
            with logfile.open("wb") as log:
                row["native_exit"] = call([
                    *run, "build", flag, "--spawn_strategy=local", "--strategy=ObserveWorkerRoot=remote",
                    "--noremote_local_fallback", "--noremote_accept_cached", "--noremote_upload_local_results",
                    "--remote_retries=0", "--remote_download_outputs=toplevel",
                    "--invocation_id="+row["invocation"], "--build_event_json_file="+str(bep),
                    "--execution_log_json_file="+str(spawns),
                    *["--remote_default_exec_properties="+k+"="+v for k,v in properties.items()],
                    PACKAGE+"observe", PACKAGE+"observer"], log=log).returncode
            log_hash = hashlib.sha256()
            detected = set()
            with logfile.open("rb") as lines:
                for line in lines:
                    log_hash.update(line)
                    for name in ("mount overlayfs: invalid argument", "permission denied", "manifest unknown"):
                        if name.encode() in line.lower():
                            detected.add(name)
            row["log_sha256"] = log_hash.hexdigest()
            row["diagnostics"] = sorted(detected)
            save()
            if row["native_exit"]:
                row["status"] = "BUILD_FAILED"
                continue
            row["evidence"] = verify.action(argparse.Namespace(
                arch=arch, layout=layout, repository=args.repository, invocation=row["invocation"],
                execroot=bazel_path(local, "info", "execution_root"), bep=bep, log=spawns))
            row["status"] = "PASS"
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
            row["verification_error"] = type(exc).__name__
        finally:
            save()
    return all(row["status"] == "PASS" for row in result["architectures"].values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bazel", type=Path, required=True)
    parser.add_argument("--rc", type=Path, required=True, help="Private coordinator RC from the approved RBE client")
    parser.add_argument("--work", type=Path, required=True, help="Empty private hosted-job directory")
    parser.add_argument("--repository", required=True, help="Public repository containing the exact emitted manifests")
    args = parser.parse_args()
    verify.require(re.fullmatch(r"[a-z0-9.-]+/[a-z0-9._/-]+", args.repository)
                   and not any(p in (".","..","") for p in args.repository.split("/")), "public image repository")
    verify.require(not args.work.exists() or not any(args.work.iterdir()), "work directory is not empty")
    raise SystemExit(0 if run(args) else 1)
