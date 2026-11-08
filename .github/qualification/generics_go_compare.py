"""Run the existing generator tests and exercise their actual target executable."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

BASE = "975fb172241c7946a9f67b4f52e0349d9240589a"
FIX = "957b1bd2d6fdb4b244cab3a649ab5d8566cb6a67"
TREE = "a69c8815b87090c2c59e8319fa2c111f6e7ce1c2"
P = "tools/go_generics/tests"
NAMES = ("all_stmts", "all_types", "anon", "consts", "imports", "remove_typedef", "simple")
LABELS = [f"//{P}/{n}:{n}_test" for n in NAMES]
TOOL = f"//{P}:compare"
QEMU = Path("/usr/bin/qemu-aarch64-static")
W = Path(os.environ["RUNNER_TEMP"]) / "golden"
OUT = W / "receipt"
REPORT = {"carrier": os.environ["GITHUB_SHA"], "source": FIX, "base": BASE, "tree": TREE, "cells": {}}


def save():
    (OUT / "result.json").write_text(json.dumps(REPORT, indent=2, sort_keys=True) + "\n")


def check(name, function, *args):
    row = REPORT["cells"][name] = {"verified": False}
    try:
        function(row, *args)
        row["verified"] = True
    except Exception as err:
        row["error"] = f"{type(err).__name__}: {err}"[:450]
    save()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def materialized(item):
    uri = urlsplit(item["uri"])
    assert uri.scheme == "file" and not uri.netloc
    return Path(unquote(uri.path))


def members(group, groups):
    yield from group.get("files", [])
    for ref in group.get("fileSets", []):
        yield from members(groups[ref["id"]], groups)


def owners(row, arch, negative=False):
    labels = LABELS[-1:] if negative else LABELS
    phase = arch + ("-mutated" if negative else "")
    bep = W / (phase + ".bep")
    opts = ["--config=" + ("x86_64" if arch == "amd64" else "aarch64"), "--jobs=4",
            "--remote_cache=", "--remote_executor=", "--bes_backend=", "--lockfile_mode=off",
            "--spawn_strategy=processwrapper-sandbox,local", "--strategy=GoStdlib=processwrapper-sandbox",
            "--strategy=GoStdlibList=processwrapper-sandbox", "--strategy=TestRunner=local",
            "--nocache_test_results", "--flaky_test_attempts=1", "--test_sharding_strategy=disabled"]
    if arch == "arm64":
        opts += ["--@bazel_tools//tools/test:incompatible_use_default_test_toolchain=false", "--run_under=" + str(QEMU)]
    cmd = [str(W / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g", "test",
           *opts, "--build_event_json_file=" + str(bep), *labels, TOOL]
    golden = Path(P) / "simple/output.go"
    original = golden.read_bytes()
    try:
        if negative:
            old, new = b"_ = new(Q) // escapes: test annotation.", b"_ = new(Q) // unexpected annotation."
            assert original.count(old) == 1
            golden.write_bytes(original.replace(old, new))
        with (W / (phase + ".log")).open("w") as log:
            row["bazel_exit"] = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
    finally:
        if negative:
            golden.write_bytes(original)
    if row["bazel_exit"] != (3 if negative else 0):
        lines = (W / (phase + ".log")).read_text(errors="replace").splitlines()
        row["errors"] = [line[:350] for line in lines if line.startswith("ERROR:")][:5]
    events = [json.loads(line) for line in bep.open()]
    row["invocation"], = [e["started"]["uuid"] for e in events if "started" in e]
    groups, complete = {}, {}
    tests = row["tests"] = {}
    for e in events:
        key = e["id"]
        if "namedSet" in key:
            groups[key["namedSet"]["id"]] = e["namedSetOfFiles"]
        if "targetCompleted" in key and "completed" in e:
            k = key["targetCompleted"]
            if k["label"] in [*labels, TOOL] and not k.get("aspect"):
                complete[k["label"]] = (k["configuration"]["id"], e["completed"])
        if "testResult" not in key or "testResult" not in e:
            continue
        k, v = key["testResult"], e["testResult"]
        label, info = k["label"], v["executionInfo"]
        assert label in labels and label not in tests
        r = tests[label] = {"status": v["status"], "strategy": info["strategy"], "config": k["configuration"]["id"],
                            "cached": bool(v.get("cachedLocally") or info.get("cachedRemotely"))}
        for f in v["testActionOutput"]:
            name = f["name"]
            if name not in ("test.log", "test.xml"):
                continue
            data = materialized(f).read_bytes()
            assert 0 < len(data) < 65536 and f.get("digest", sha(data)) == sha(data)
            (OUT / (phase + "." + label.rsplit(":", 1)[1] + "." + name)).write_bytes(data)
            r[name] = sha(data)
            if name == "test.xml":
                ET.fromstring(data)
            elif negative:
                assert b"unexpected annotation." in data and b"escapes: test annotation." in data
    assert set(tests) == set(labels) and row["bazel_exit"] == (3 if negative else 0)
    for label, r in tests.items():
        assert r["status"] == ("FAILED" if negative else "PASSED") and r["strategy"] == "local" and not r["cached"]
        assert r.get("test.log") and r.get("test.xml") and complete[label][0] == r["config"]
    if negative:
        return
    outputs = row["default_sha256"] = {}
    for label in [*labels, TOOL]:
        config, result = complete[label]
        assert result["success"] and config
        group, = [g for g in result["outputGroup"] if g["name"] == "default"]
        f, = members(group, groups)
        path = materialized(f)
        data = path.read_bytes()
        assert data[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", data, 18)[0] == (62 if arch == "amd64" else 183)
        outputs[label] = sha(data)
        if label == TOOL:
            shutil.copy2(path, W / ("compare-" + arch))
    assert len(set(outputs.values())) == 1



def controls(row, arch):
    root = W / ("empty-" + arch)
    root.mkdir()
    shutil.copy2(W / ("compare-" + arch), root / "compare")
    prefix = ["/compare"]
    if arch == "arm64":
        shutil.copy2(QEMU, root / "qemu")
        prefix.insert(0, "/qemu")
    cmd = ["sudo", "-n", "chroot", f"--userspec={os.getuid()}:{os.getgid()}", str(root), *prefix]
    args = ["expected input.go", "generated input.go"]
    line = "var x = 1\n"
    cases = [
        ("identical", line, line, 0), ("blank", line, "\n" + line + "\n", 0),
        ("comments", "package p\n\t// left\n", "package p\n // right\n", 0),
        ("code", line, "var x = 2\n", 1), ("annotation", "x() // old\n", "x() // new\n", 1),
        ("code-to-comment", line, "//" + line, 1), ("spaces", line, line + " \t\n", 1),
        ("final-newline", line, line.rstrip("\n"), 1), ("missing-input", None, line, 2),
    ]
    row["binary"] = sha((root / "compare").read_bytes())
    row["cases"] = {}
    for name, before, after, expected in cases:
        for f, data in zip(args, [before, after]):
            p = root / f
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(data.encode())
        go = subprocess.run([*cmd, *args], capture_output=True)
        gnu = subprocess.run(["/usr/bin/diff", "--ignore-blank-lines", "--ignore-matching-lines=^[[:space:]]*//",
                              *args], cwd=root, capture_output=True)
        row["cases"][name] = {"go": go.returncode, "gnu": gnu.returncode, "stderr": go.stderr.decode()[:760]}
        assert go.returncode == gnu.returncode == expected, (name, go.returncode, gnu.returncode)
        assert not go.stdout
        if expected == 1:
            assert all(f.encode() in go.stderr for f in args) and go.stderr.partition(b"\n")[2].strip()
        if name == "code":
            assert b"var x = 1" in go.stderr and b"var x = 2" in go.stderr
        if name == "code-to-comment":
            assert b"var x = 1" in go.stderr
        if expected == 2:
            assert args[0].encode() in go.stderr
    assert not (root / "bin").exists()


def main():
    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()
    assert git("rev-parse", "HEAD") == REPORT["carrier"] and git("rev-parse", "HEAD^") == FIX
    assert git("rev-parse", FIX + "^") == BASE and git("rev-parse", FIX + "^{tree}") == TREE
    assert git("diff", "--name-only", BASE, FIX).splitlines() == [P + "/" + f for f in ("BUILD", "compare.go", "defs.bzl")]
    assert os.uname().machine == "x86_64"
    REPORT["qemu"] = {"version": subprocess.check_output([QEMU, "--version"], text=True).splitlines()[0],
                      "sha256": sha(QEMU.read_bytes())}
    save()
    for arch in ("amd64", "arm64"):
        check(arch + "-owners", owners, arch)
        check(arch + "-controls", controls, arch)
    check("real-annotation-mismatch", owners, "amd64", True)
    return all(r["verified"] for r in REPORT["cells"].values())


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
