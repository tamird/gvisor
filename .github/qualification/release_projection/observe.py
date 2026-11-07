"""Read the configured release producers and compare a tiny local staging action."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

W = Path(__file__).resolve().parent
PRODUCT = os.environ["PRODUCT"]
STAGE = "//debian:release-inputs"
OLD = "//debian:release-archives"
DIST = "//sandboxexec/sandbox/python:dist"
ARCHES = {"x86_64": ("k8", "linux_amd64"), "aarch64": ("aarch64", "linux_arm64")}
PRODUCERS = {"//debian:debian": "runsc.deb", "//debian:gvisor-release-tar-bz2": "gvisor.tar.bz2",
             "//debian:gvisor-release-tar-zstd": "gvisor.tar.zstd"}
LEAVES = {arch + "/" + name for arch in ARCHES for name in PRODUCERS.values()}
TINY = "//test/rbe/stamped_release:boundary-inputs"
LABELS = ["--remote_executor=", "--remote_cache=", "--bes_backend=", "--lockfile_mode=off",
          "--jobs=3", "--noshow_progress", "--color=no", "--curses=no"]


def save(name, row):
    data = json.dumps({"source": PRODUCT, **row}, indent=2)
    assert len(data) < 70000
    (W / "public" / (name + ".json")).write_text(data + "\n")


def diag(data):
    lines = data.decode(errors="replace").splitlines()
    selected = [line for line in lines if re.search(r"ERROR:|Error in |not a regular |unexpected Python|^open |^stat |failed to|No such file|missing required", line, re.I)
                and not re.search(r"https?://\S+\?|authorization|token|api.key", line, re.I)]
    return [line.replace(str(W), "<work>").replace(str(Path.home()), "<home>")[:380] for line in selected[:7]]


def bazel(command, *args):
    return subprocess.run([W / "bazel", "--nosystem_rc", "--nohome_rc", command, *LABELS, *args],
                          capture_output=True)


def label(value):
    return value.lstrip("@")


def graph(mode, row):
    targets = f"set({OLD} {STAGE})"
    opts = ["-c", "opt", "--remote_download_outputs=toplevel",
            "--//sandboxexec/sandbox/python:dist_version=" + ("2026.10.26.13" if mode == "tagged" else "")]
    names = [*PRODUCERS, DIST, STAGE, OLD]
    pattern = "(" + "|".join(names) + ")$"
    expr = r'"\t".join([str(target.label), str(build_options(target)["//command_line_option:cpu"]), str(build_options(target)["//command_line_option:platforms"]), ",".join(sorted([f.path for f in target.files.to_list()]))])'
    p = bazel("cquery", *opts, "--output=starlark", "--starlark:expr=" + expr,
              f'filter("{pattern}", deps({targets}))')
    row["cquery"] = {"exit": p.returncode, "diagnostic": diag(p.stderr)}
    if p.returncode:
        raise RuntimeError("cquery failed")
    entries = []
    for line in p.stdout.decode().splitlines():
        owner, cpu, platform, paths = line.split("\t")
        entries.append({"owner": label(owner), "cpu": cpu, "platform": platform, "files": paths.split(",")})
    row["configured"] = entries
    by_label = lambda owner: [entry for entry in entries if entry["owner"] == owner]
    old, = by_label(OLD)
    final, = by_label(STAGE)
    assert {p.split("/debian/release-archives/")[1] for p in old["files"]} == LEAVES
    originals = {}
    for arch, (cpu, platform) in ARCHES.items():
        for owner, filename in PRODUCERS.items():
            cell, = [entry for entry in by_label(owner) if entry["cpu"] == cpu and platform in entry["platform"]]
            path, = cell["files"]
            assert path.endswith("/" + filename)
            originals[arch + "/" + filename] = path
    assert len(set(originals.values())) == 6
    assert len(by_label(DIST)) == (1 if mode == "tagged" else 0)
    root, = {p.partition("/debian/release-inputs/")[0] + "/debian/release-inputs/"
             for p in final["files"] if "/debian/release-inputs/" in p}
    staged = LEAVES | {p + ".sha512" for p in LEAVES if not p.endswith(".deb")} | {"inputs", "python"}
    assert set(final["files"]) == {root + p for p in staged} | set(originals.values())
    aq = 'mnemonic("^(Symlink|FileWrite|ReleaseSHA512|StageReleaseDist|EmptyReleaseDist)$", ' + targets + ') union mnemonic("^SandboxExecDist$", deps(' + STAGE + '))'
    p = bazel("aquery", *opts, "--output=jsonproto", "--include_file_write_contents", aq)
    row["aquery"] = {"exit": p.returncode, "diagnostic": diag(p.stderr)}
    if p.returncode:
        raise RuntimeError("aquery failed")
    d = json.loads(p.stdout)
    targets_by_id = {str(x["id"]): label(x["label"]) for x in d["targets"]}
    fragments = {str(x["id"]): x for x in d["pathFragments"]}
    def path_of(i):
        f = fragments[str(i)]
        return (path_of(f["parentId"]) + "/" if f.get("parentId") else "") + f["label"]
    paths = {str(x["id"]): path_of(x["pathFragmentId"]) for x in d["artifacts"]}
    sets = {str(x["id"]): x for x in d["depSetOfFiles"]}
    def inputs(i):
        v = sets[str(i)]
        return {paths[str(n)] for n in v.get("directArtifactIds", [])}.union(
            *(inputs(x) for x in v.get("transitiveDepSetIds", [])))
    observed = []
    for action in d["actions"]:
        owner = targets_by_id[str(action["targetId"])]
        kind = action["mnemonic"]
        out = paths[str(action["primaryOutputId"])]
        observed.append({"owner": owner, "type": kind, "out": out,
                         "configuration": action["configurationId"]})
        if kind == "Symlink":
            relative = out.partition("/debian/" + owner.rsplit(":", 1)[1] + "/")[2]
            assert {originals[relative]} == set().union(*(inputs(x) for x in action["inputDepSetIds"]))
        elif kind == "ReleaseSHA512":
            relative = out.removeprefix(root).removesuffix(".sha512")
            assert action["arguments"][-3:] == ["checksum", originals[relative], out]
        elif kind == "FileWrite":
            assert owner == STAGE and out == root + "inputs"
            values = action["fileContents"].split("\0")
            assert values[-1] == "" and len(values) == 7 and set(values[:-1]) == LEAVES
        elif kind in ("EmptyReleaseDist", "StageReleaseDist"):
            assert out == root + "python"
            args = action["arguments"]
            if mode == "tagged":
                dist_path, = by_label(DIST)[0]["files"]
                assert args[-3:] == ["python", dist_path, out]
            else:
                assert args[-2:] == ["empty", out]
    row["actions"] = observed
    from collections import Counter
    expected = {"Symlink": 12, "FileWrite": 1, "ReleaseSHA512": 4}
    expected.update({"StageReleaseDist": 1, "SandboxExecDist": 1} if mode == "tagged" else {"EmptyReleaseDist": 1})
    assert Counter(a["type"] for a in observed) == expected


def make(row):
    missing = "aarch64/gvisor.tar.zstd"
    key = W / "unused.key"
    key.touch()
    for mode, stage in (("default", Path("artifacts")), ("prebuilt", W / "prebuilt")):
        for path in LEAVES - {missing}:
            target = stage / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        base = ["make", "--no-print-directory", "RELEASE_KEY=" + str(key), "RELEASE_ROOT=" + str(W / "never-signed")]
        args = ["DOCKER_BUILD=false"] if mode == "default" else ["RELEASE_ARTIFACTS=" + str(stage)]
        printed = subprocess.run([*base, "-n", *args, "release"], capture_output=True)
        text = printed.stdout.decode()
        assert printed.returncode == 0
        assert ("//debian:release-archives" in text) == (mode == "default")
        assert all(path in text for path in LEAVES)
        # Exercise the actual host adapter up to its final mandatory input.
        # The original artifacts-all producer was checked independently on #16490.
        exec_args = [*args, "-o", "artifacts-all"] if mode == "default" else args
        p = subprocess.run([*base, *exec_args, "release"], capture_output=True)
        message = f"Missing release file: {stage}/{missing}"
        row[mode] = {"exit": p.returncode, "missing": message in p.stderr.decode(),
                     "build_in_dry_run": "//debian:release-archives" in text, "build_suppressed": mode == "default",
                     "all_six_requested": all(path in text for path in LEAVES)}
        assert row[mode]["missing"] and p.returncode and not (W / "never-signed").exists()


def fixture(row):
    pkg = Path("test/rbe/stamped_release")
    pkg.mkdir(parents=True)
    shutil.copyfile(W / "boundary.bzl", pkg / "boundary.bzl")
    buildfile = pkg / "BUILD"
    source = (W / "BUILD.fixture").read_text()
    buildfile.write_text(source)
    binary = Path("tools/release/stage.go")
    pristine = binary.read_bytes()
    prim = subprocess.check_output(["git", "hash-object", binary],text=True).strip()
    assert prim == "5ae35db4fde30b5f83e2c730a7d2e0cbbcf31193"
    try:
        for name in ("original", "fixed", "empty"):
            if name == "fixed":
                subprocess.run(["git", "apply", "--unidiff-zero", W / "regular-input.patch"], check=True)
                assert subprocess.check_output(["git", "hash-object", binary],text=True).strip() == "ced30714042ac610e9d736448b278ca5991233e8"
            if name == "empty":
                buildfile.write_text(source.replace('    python_dist = ":boundary-tree",\n', ''))
            bep = W / (name + ".bep")
            p = bazel("build", "--spawn_strategy=linux-sandbox", "--strategy=GoStdlib=processwrapper-sandbox",
                      "--strategy=GoStdlibList=processwrapper-sandbox", "--build_event_publish_all_actions",
                      "--build_event_json_file=" + str(bep), TINY)
            actions = []
            for line in bep.read_text().splitlines() if bep.exists() else []:
                event = json.loads(line)
                owner = event["id"].get("actionCompleted", {})
                if label(owner.get("label", "")) == TINY:
                    a = event["action"]
                    actions.append({"type": a.get("type"), "success": a.get("success",False),
                                    "primary": owner.get("primaryOutput")})
            cell = {"exit": p.returncode, "actions": actions, "stderr_sha256": hashlib.sha256(p.stderr).hexdigest(),
                    "diagnostic": diag(p.stderr)}
            if p.returncode:
                lines = p.stderr.decode(errors="replace").splitlines()
                command = next((i for i, line in enumerate(lines) if re.search(r"[/ ]stage['\"]?\s+python\s", line)), None)
                if command is not None:
                    cell["stage_context"] = [line.replace(str(W), "<work>").replace(str(Path.home()), "<home>")[:380]
                                             for line in lines[command + 1:command + 9]
                                             if not re.search(r"https?://\S+\?|authorization|token|api.key|credential", line, re.I)]
            row[name] = cell
            save("fixture", row)
            if name == "original":
                cell["original_failure_matched"] = bool(p.returncode and
                    re.search(rb"not a regular distribution: gvisor-1\.0(?:-py3-none-any\.whl|\.tar\.gz)",p.stderr) and
                    any(a["type"] == "StageReleaseDist" and not a["success"] for a in actions))
                continue
            assert p.returncode == 0 and any(a["success"] and a["type"] == ("StageReleaseDist" if name == "fixed" else "EmptyReleaseDist") for a in actions)
            out = Path("bazel-bin/test/rbe/stamped_release/boundary-inputs")
            dist = out / "python"
            cell["dir"] = str(dist.resolve())
            cell["members"] = sorted(p.name for p in dist.iterdir())
            if name == "fixed":
                names = {"gvisor-1.0-py3-none-any.whl","gvisor-1.0.tar.gz"}
                assert set(cell["members"]) == names | {n+".sha512" for n in names}
                for n in names:
                    assert (dist / n).read_bytes() == n.encode()
                    subprocess.run(["sha512sum","--check","--strict","--",n+".sha512"], cwd=dist, capture_output=True, check=True)
                for arch in ARCHES:
                    subprocess.run(["sha512sum","--check","--strict","--","-archive, space\n.sha512"],
                                   cwd=out / arch, capture_output=True, check=True)
                assert set((out/"inputs").read_bytes().split(b"\0")) == {b"",*(f"{a}/{n}".encode() for a in ARCHES for n in ("-archive, space\n","runsc.deb"))}
            else:
                assert not cell["members"] and cell["dir"] == row["fixed"]["dir"]
                deny = W / "deny-gcloud"; deny.mkdir()
                g = deny / "gcloud"
                g.write_text('#!/bin/sh\ntouch "$FORBIDDEN_GATE"\nexit 125\n'); g.chmod(0o755)
                p = subprocess.run(["tools/make_python_release.sh","publish-manifest",dist],capture_output=True,
                                   env={**os.environ,"PATH":str(deny)+":/usr/bin:/bin","FORBIDDEN_GATE":str(deny/"called")})
                cell["gate_skipped"] = p.returncode == 0 and b"No wheel staged" in p.stdout and not (deny/"called").exists()
                assert cell["gate_skipped"]
        assert row["original"]["original_failure_matched"]
    finally:
        binary.write_bytes(pristine)


def run(name, fn, *args):
    row = {"verified": False}
    try:
        fn(*args, row)
        row["verified"] = True
    except Exception as exc:
        import traceback
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        row["error"] = {"type":type(exc).__name__, "line":frame.lineno, "detail":str(exc)[:280]}
    save(name, row)
    print(name, row["verified"], flush=True)
    return row["verified"]


if __name__ == "__main__":
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip() == PRODUCT
    mode = sys.argv[1]
    if mode == "graphs":
        outcomes = [run("graph-"+m,graph,m) for m in ("tagged","empty")]
    else:
        outcomes = [run(mode,{"make":make,"fixture":fixture}[mode])]
    sys.exit(not all(outcomes))
