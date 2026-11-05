"""Compare the installed Go offsets and stub bytes with their source ELF."""
import json
from pathlib import Path
import re
import struct
import sys

import native_cross_inputs as q

BASE = "add07b5fa1fdf48d20ed9b39277e1ebcc75b0aaf"
PRODUCT = "b540bc61643afb589a8d465ed5c7b083cd6d3fa0"
FLAGS = ["--config=x86_64", "--jobs=4", "--remote_cache=", "--remote_executor=", "--disk_cache="]
q.ROOTS = [q.SYSMSG + "sighandler_binary_arch", q.SYSMSG + "sighandler_go_arch"]


def run(row, key, args, cap):
    rc, out, err = q.command(args, cap)
    row[key] = {"exit": rc, "diagnostics": q.diagnostics(err)}
    if rc:
        raise ValueError(key)
    return out.decode().strip()


def compare_outputs(label, sha, row):
    q.source(sha)
    bep = q.TASK / (label + ".bep")
    cmd = [q.BAZEL, "build", *FLAGS, "--build_event_publish_all_actions", "--build_event_json_file=" + str(bep), *q.ROOTS]
    try:
        run(row, "build", cmd, 1000)
    finally:
        row["events"] = q.events(bep)
    events = row["events"]
    assert events["exit"]["name"] == "SUCCESS" and {r["owner"] for r in events["targets"] if r["success"]} == set(q.ROOTS)
    if label == "candidate":
        actions = [r for r in events["actions"] if r["owner"] == q.SYSMSG + "sighandler_data" and r["success"] and r["exit"] == 0]
        assert len({r["configuration"] for r in actions}) == 2
    root = Path(run(row, "execution_root", [q.BAZEL, "info", "execution_root"], 60))
    roots = "set(" + " ".join(q.ROOTS) + ")"
    outputs = run(row, "outputs", [q.BAZEL, "cquery", *FLAGS, "--output=files", roots], 180).splitlines()
    objects = run(row, "objects", [q.BAZEL, "cquery", *FLAGS, "--output=files",
                                    'filter(":sighandler_object$", deps(' + roots + '))'], 180).splitlines()
    assert len(outputs) == 4 and len(set(objects)) == 2
    elfs = {}
    for obj in sorted(set(objects)):
        meta, blob, exports = q.elf(q.read_file(root, obj))
        assert meta["machine"] in (62, 183) and blob and exports
        elfs[meta["machine"]] = (meta, blob, exports)
    assert set(elfs) == {62, 183}
    paired = {}
    for machine, arch in ((62, "amd64"), (183, "arm64")):
        meta, blob, exports = elfs[machine]
        raw, = [q.read_file(root, p) for p in outputs if Path(p).name == f"sighandler.built-in.{arch}.bin"]
        go, = [q.read_file(root, p) for p in outputs if Path(p).name == f"sighandler_{arch}.go"]
        fields = re.findall(rb'^var Sighandler_blob_offset__(\w+) = (0x[0-9a-fA-F]+)$', go, re.M)
        offsets = {name.decode():int(value, 16) for name, value in fields}
        assert len(fields) == len(exports) and offsets == exports and raw == blob
        paired[arch] = {"elf": meta, "bytes": len(blob), "sha256": q.digest(blob), "offsets": offsets}
    row["architectures"] = paired
    return {arch:(x["sha256"], x["offsets"]) for arch, x in paired.items()}


result = {"base": BASE, "product": PRODUCT, "scope": "GNU, native AMD64 worker with both target architectures"}
try:
    results = []
    for label, commit in (("base", BASE), ("candidate", PRODUCT)):
        result[label + "_run"] = {}
        results.append(compare_outputs(label, commit, result[label + "_run"]))
        q.write(result)
    assert results[0] == results[1]
    result["matched"] = True
except (OSError, ValueError, KeyError, TypeError, AssertionError, struct.error) as e:
    result["error"] = {"class": type(e).__name__, "message": str(e)[:160]}
q.write(result)
sys.exit(not result.get("matched", False))
