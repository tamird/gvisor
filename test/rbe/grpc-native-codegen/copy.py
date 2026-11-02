"""Check the supplier executable at the real codegen, packaging and buildgen boundaries."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile

SOURCE = "88ac70ff98fec233528eab6658e4b0945c9f062e"
BASE = "db13f8422846404ffa2e81e5d76d19488df30586"
TEMP = Path(os.environ["RUNNER_TEMP"])
ROOT = Path.cwd()
SUPPLIER = ROOT / "supplier"
LANE = sys.argv[1]
RECEIPT = TEMP / (LANE + "-receipt.json")
report = {"phase": LANE, "source": SOURCE, "base": BASE, "status": "INCOMPLETE",
          "os": platform.system(), "arch": platform.machine(), "started": time.time(), "commands": []}


def save():
    RECEIPT.write_text(json.dumps(report, indent=2) + "\n")


def output(*args, cwd=SUPPLIER):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(name, args, cwd=SUPPLIER, stdout=None):
    log = TEMP / (LANE + "-" + name + ".log")
    start = time.time()
    with log.open("wb") as err:
        res = subprocess.run(args, cwd=cwd, stdout=stdout or err, stderr=err)
    row = {"name": name, "start": start, "finish": time.time(), "exit": res.returncode}
    with log.open(errors="replace") as f:
        matches = []
        for line in f:
            if "Invocation ID:" in line:
                row["uuid"] = line.split("Invocation ID:")[-1].strip()
            if res.returncode and re.search(r"\b(error|failed|fatal|exception)\b", line, re.I):
                if len(matches) < 10 and not re.search(r"token|authorization|credential|remote_header|x-buildbuddy", line, re.I):
                    matches.append(re.sub(r"https?://\S+", "<url>", line.strip())[:820])
        if matches:
            row["diagnostic"] = matches
    report["commands"].append(row)
    save()
    if res.returncode:
        raise RuntimeError(name + " exited " + str(res.returncode))


def linux():
    import measure as prior

    module = ROOT / "MODULE.bazel"
    old = module.read_bytes()
    assert old == subprocess.check_output(["git", "show", prior.base + ":MODULE.bazel"])
    targets = list(prior.services)
    flags = [*prior.options, "--experimental_add_exec_constraints_to_targets=^.*_grpc_codegen$=@grpc_workers//:empty"]
    module.write_bytes(old + prior.host_module + ('\nlocal_path_override(module_name="grpc",path="' + str(SUPPLIER) + '")\n').encode())
    try:
        graph_path = TEMP / "copy-plan.json"
        query = 'mnemonic("Action|Genrule|ExecutableSymlink", deps(set(' + " ".join(targets) + ')))'
        with graph_path.open("wb") as out:
            run("plan", [*prior.bazel, "aquery", *flags, "--output=jsonproto", "--noinclude_artifacts", query], ROOT, out)
        graph = json.loads(graph_path.read_bytes())
        labels = {t["id"]: prior.label(t["label"]) for t in graph["targets"]}
        actions = [(labels[a["targetId"]], a) for a in graph["actions"]]
        wrapper_actions = [a["mnemonic"] for owner, a in actions if owner == prior.wrapper]
        assert wrapper_actions == ["ExecutableSymlink"], wrapper_actions
        selected = {owner: [v for v in a["arguments"] if v.startswith("--plugin=")]
                    for owner, a in actions if owner in targets and a["mnemonic"] == "Action"}
        assert set(selected) == set(targets) and all(len(v) == 1 and v[0].endswith("/grpc_cpp_plugin_binary") for v in selected.values())
        report["selected"] = {"wrapper_actions": wrapper_actions, "plugin": selected}
        save()
        spawns = TEMP / "copy-spawns.json"
        build = [*prior.bazel, "build", *flags, "--execution_log_json_file=" + str(spawns), *targets]
        run("generate", build, ROOT)
        accesses = []
        for s in prior.spawns(spawns):
            owner = prior.label(s.get("targetLabel", ""))
            if owner in targets and s.get("mnemonic") == "Action":
                props = {p["name"]: p["value"] for p in s["platform"]["properties"]}
                accesses.append({"owner": owner, "runner": s["runner"], "exit": s["exitCode"],
                                 "cached": s.get("cacheHit", False) or s.get("remoteCacheHit", False),
                                 "image": props.get("container-image"), "isolation": props.get("workload-isolation-type"),
                                 "plugin": [v for v in s["commandArgs"] if v.startswith("--plugin=")]})
        report["executions"] = accesses
        expected = json.loads((ROOT / "test/rbe/grpc-native-codegen/result.json").read_bytes())["matches"]
        actual = {p: {"size": (ROOT / "bazel-bin" / p).stat().st_size, "sha256": digest(ROOT / "bazel-bin" / p)} for p in expected}
        report["outputs"] = actual
        save()
        assert sorted(a["owner"] for a in accesses) == sorted(targets)
        assert all(a["runner"] == "remote" and not a["exit"] and not a["cached"]
                   and a["image"] == "docker://" + os.environ["MINIMAL"] and a["isolation"] == "oci"
                   and a["plugin"] == selected[a["owner"]] for a in accesses)
        assert actual == expected
    finally:
        module.write_bytes(old)


def metadata():
    import yaml

    manifest = SUPPLIER / "build_autogenerated.yaml"
    stock = manifest.read_bytes()
    report["checked_in_sha256"] = digest(manifest)
    # Some upstream commits update this tracked manifest separately from their BUILD changes.
    # Compare the canonical extractor on both actual sources, even if the stored file is stale.
    for name, ref in [("base", BASE), ("candidate", SOURCE)]:
        run("select-" + name, ["git", "checkout", "--detach", ref])
        assert output("git", "rev-parse", "HEAD") == ref
        run("manifest-" + name, ["python3", "tools/buildgen/extract_metadata_from_bazel_xml.py"])
        manifest_hash = digest(manifest)
        labels = [t["name"] for t in yaml.safe_load(manifest.read_bytes())["targets"]
                  if t["name"].startswith("grpc_") and t["name"].endswith("_plugin")]
        report[name] = {"sha256": manifest_hash, "plugin_names": sorted(labels)}
        save()
        manifest.write_bytes(stock)
    assert report["base"] == report["candidate"]
    assert len(report["candidate"]["plugin_names"]) == 7


def darwin():
    assert platform.system() == "Darwin"
    bazel = [str(SUPPLIER / "tools/bazel")]
    names = ["grpc_cpp_plugin", "grpc_objective_c_plugin"]
    targets = ["//src/compiler:" + n for n in names]
    proto = "@com_google_protobuf//:protoc"
    flags = ["--jobs=4", "--copt=-g0", "--host_copt=-g0"]
    run("build", [*bazel, "build", *flags, *targets, proto])
    root = Path(output(*bazel, "info", "execution_root"))
    protoc = root / output(*bazel, "cquery", *flags, "--output=files", proto)
    dist = TEMP / "dist"
    dist.mkdir()
    shutil.copy(protoc, dist / "protoc")
    shutil.copy(SUPPLIER / "examples/protos/helloworld.proto", dist)
    originals = []
    for n in names:
        p = SUPPLIER / "bazel-bin/src/compiler" / (n + "_binary")
        original = p.resolve(strict=True)
        assert original.name == n + "_universal", original
        originals.append(original)
        archive = dist / (n + ".zip")
        run("archive-" + n, ["/usr/bin/zip", "-j", str(archive), str(p)])
        with zipfile.ZipFile(archive) as z:
            info, = z.infolist()
            assert info.filename == p.name and stat.S_ISREG(info.external_attr >> 16)
        run("install-" + n, ["/usr/bin/unzip", "-q", str(archive)], dist)
        installed = dist / p.name
        arches = sorted(output("/usr/bin/lipo", "-archs", str(installed)).split())
        assert arches == ["arm64", "x86_64"] and not installed.is_symlink() and os.access(installed, os.X_OK)
        assert digest(p) == digest(installed)
        report[n] = {"name": p.name, "original_target": original.name, "archive_sha256": digest(archive),
                     "installed_sha256": digest(installed), "architectures": arches}
        save()
    run("remove-build-tree", [*bazel, "clean", "--expunge"])
    assert not any(p.exists() for p in originals)
    for n in names:
        run("installed-" + n, [str(dist / "protoc"), "--plugin=protoc-gen-grpc=" + str(dist / (n + "_binary")),
                               "--grpc_out=.", "helloworld.proto"], dist)
    generated = {p.name: digest(p) for p in dist.iterdir() if p.suffix in (".h", ".m", ".cc") and b"sayhello" in p.read_bytes().lower()}
    report["generated"] = generated
    assert sorted(s.lower() for s in generated) == ["helloworld.grpc.pb.cc", "helloworld.grpc.pb.h", "helloworld.pbrpc.h", "helloworld.pbrpc.m"]


if __name__ == "__main__":
    save()
    try:
        assert output("git", "rev-parse", "HEAD") == SOURCE and output("git", "rev-parse", "HEAD^") == BASE
        {"linux": linux, "darwin": darwin, "metadata": metadata}[LANE]()
        report["status"] = "PASS"
    except Exception as error:
        report["error"] = str(error)[:1200]
        raise
    finally:
        report["finished"] = time.time()
        save()
