# Copyright 2026 The gVisor Authors.
# SPDX-License-Identifier: Apache-2.0
"""Build one local Bazel OCI layout; retain only its checked public blobs."""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import uuid

sys.path.insert(0, "test/rbe/directory_worker")
from verify import inventory, require

ROOT = Path(os.environ["RUNNER_TEMP"]) / "directory-layout"
OUT = ROOT / "out"
OWNER = "//test/rbe/directory_worker:layout"
PRODUCT = "fa91c2d64c4a4cb1a449e722de3c662071262104"


def checked_archive(layout, images):
    root = (layout / "index.json").read_bytes()
    descs = json.loads(root)["manifests"]
    members = {"index.json": root, "oci-layout": (layout / "oci-layout").read_bytes()}
    require(json.loads(members["oci-layout"]) == {"imageLayoutVersion": "1.0.0"}, "OCI version")

    def add(desc):
        algo, hexdigest = desc["digest"].split(":")
        require(algo == "sha256" and re.fullmatch("[0-9a-f]{64}", hexdigest), "descriptor")
        name = "blobs/sha256/" + hexdigest
        data = (layout / name).read_bytes()
        require(len(data) == desc["size"] and hashlib.sha256(data).hexdigest() == hexdigest, "OCI blob")
        members[name] = data
        return data

    if len(descs) == 1:
        index = add(descs[0])
        descs = json.loads(index)["manifests"]
    else:
        index = root
    require({d["digest"] for d in descs} == {v["manifest"] for v in images.values()}, "index")
    manifest_sizes = {}
    for d in descs:
        manifest = json.loads(add(d))
        arch = json.loads(add(manifest["config"]))["architecture"]
        layer, = manifest["layers"]
        add(layer)
        manifest_sizes[arch] = {"manifest_bytes": d["size"], "config": manifest["config"]["digest"],
                                "config_bytes": manifest["config"]["size"], "layer_bytes": layer["size"]}
    require(len(members) in (7, 8) and sum(map(len, members.values())) <= 32*1024, "bounded OCI closure")
    archive = ROOT / "checked-layout.tar"
    with tarfile.open(archive, "w") as tar:
        for name, data in sorted(members.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    result = {"index": "sha256:" + hashlib.sha256(index).hexdigest(), "index_bytes": len(index),
              "sizes": manifest_sizes, "tar_bytes": archive.stat().st_size,
              "tar_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    archive.replace(OUT / "layout.tar")
    return result


def collect(row):
    bazel = str(ROOT / "bazel")
    bep = ROOT / "bep"
    log = ROOT / "build.log"
    options = ["--remote_executor=", "--remote_cache=", "--bes_backend=", "--disk_cache=", "--spawn_strategy=local",
               "--lockfile_mode=off", "--jobs=4", "--color=no", "--curses=no", "--nobuild_event_json_file_path_conversion"]
    with log.open("wb") as output:
        rc = subprocess.run(["timeout", "-k", "20s", "19m", bazel, "build", *options,
                             "--build_event_json_file=" + str(bep), "--build_event_publish_all_actions",
                             "--invocation_id=" + row["invocation"], OWNER], stdout=output, stderr=subprocess.STDOUT).returncode
    row["build_exit"] = rc
    if rc:
        row["status"] = "BUILD_FAILED"
    logdata = log.read_bytes()
    row["build_log_sha256"] = hashlib.sha256(logdata).hexdigest()
    row["diagnostics"] = [d for d in ("no such attribute", "no such target", "no matching toolchains",
                                      "error downloading", "connection timed out", "permission denied", "out of memory")
                          if d.encode() in logdata.lower()]
    row["repo_locations"] = sorted({i.decode() for i in re.findall(
        rb"(?:test/rbe/directory_worker/BUILD|[a-zA-Z0-9_+-]{1,40}\.bzl):[0-9]{1,6}:[0-9]{1,5}\b", logdata)})[:8]
    row["phase"] = "Bazel result"
    events = [json.loads(line) for line in bep.open()] if bep.exists() else []
    row["failures"] = []
    for event in events:
        action = event.get("action")
        if action and not action.get("success") and len(row["failures"]) < 4:
            failure = action.get("failureDetail", {})
            row["failures"].append({"mnemonic": action.get("type"), "exit": action.get("exitCode"),
                                    "codes": [d.get("code") for d in failure.values() if isinstance(d, dict)]})
    if rc:
        row["status"] = "BUILD_FAILED"
        return
    starts = [ev["started"]["uuid"] for ev in events if "started" in ev]
    require(starts == [row["invocation"]], "BEP invocation")
    finish, = [ev["finished"] for ev in events if "buildFinished" in ev["id"]]
    require(finish["exitCode"]["name"] == "SUCCESS", "BEP exit")
    target, = [ev for ev in events if ev["id"].get("targetCompleted", {}).get("label") == OWNER
               and not ev["id"]["targetCompleted"].get("aspect")]
    require(target["completed"].get("success") is True and not any("testResult" in ev["id"] for ev in events), "BEP target")
    row["completed"] = OWNER
    row["phase"] = "OCI layout"
    query = subprocess.run([bazel, "cquery", *options, "--output=files", OWNER], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=120, text=True)
    row["cquery_exit"] = query.returncode
    require(query.returncode == 0, "cquery exit")
    path, = map(Path, query.stdout.strip().splitlines())
    require(not path.is_absolute() and path.parts[0] == "bazel-out" and ".." not in path.parts
            and path.parts[-4:] == ("test", "rbe", "directory_worker", "root_oci_layout"), "cquery layout")
    execroot = Path(subprocess.check_output([bazel, "info", "execution_root"], stderr=subprocess.DEVNULL, text=True).strip())
    layout = (execroot / path).resolve(strict=True)
    require(layout.is_dir() and layout.is_relative_to((execroot / "bazel-out").resolve(strict=True)), "layout tree")
    row["architectures"] = inventory(layout)
    row.update(checked_archive(layout, row["architectures"]))
    row.update(phase="verified", status="PASS")


if __name__ == "__main__":
    row = {"status": "INCOMPLETE", "source": PRODUCT, "invocation": str(uuid.uuid4()), "phase": "checkout",
           "worker_action_executed": False, "registry_write": False}
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        require(head == PRODUCT, "source head")
        collect(row)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as e:
        row["error"] = type(e).__name__
        if isinstance(e, ValueError) and str(e) in {"BEP invocation", "BEP exit", "BEP target", "cquery exit",
            "cquery layout", "layout tree", "config platform", "index platform", "shared physical layer",
            "sole tar member", "trailing tar data", "layer encoding", "OCI version", "bounded OCI closure"}:
            row["diagnostic"] = str(e)
    finally:
        (OUT / "receipt.json").write_text(json.dumps(row, sort_keys=True) + "\n")
    sys.exit(0 if row["status"] == "PASS" else 1)
