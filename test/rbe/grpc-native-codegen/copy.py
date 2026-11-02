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

SOURCE = "d38eb8b2ef2131b5baeaa45e9ab274e7f523bfb6"
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
    flags = [*prior.options, "--strategy=CopyGrpcPlugin=remote",
             "--modify_execution_info=^CopyGrpcPlugin$=+no-remote-cache"]
    module.write_bytes(old + prior.host_module + ('\nlocal_path_override(module_name="grpc",path="' + str(SUPPLIER) + '")\n').encode())
    try:
        graph_path = TEMP / "copy-plan.json"
        query = 'mnemonic("Action|Genrule|CopyGrpcPlugin", deps(set(' + " ".join(targets) + ')))'
        with graph_path.open("wb") as out:
            run("plan", [*prior.bazel, "aquery", *flags, "--output=jsonproto", "--noinclude_artifacts", query], ROOT, out)
        graph = json.loads(graph_path.read_bytes())
        labels = {t["id"]: prior.label(t["label"]) for t in graph["targets"]}
        actions = [(labels[a["targetId"]], a) for a in graph["actions"]]
        wrapper_actions = [a["mnemonic"] for owner, a in actions if owner == prior.wrapper]
        assert wrapper_actions == ["CopyGrpcPlugin"], wrapper_actions
        selected = {owner: [v for v in a["arguments"] if v.startswith("--plugin=")]
                    for owner, a in actions if owner in targets and a["mnemonic"] == "Action"}
        assert set(selected) == set(targets) and all(len(v) == 1 and v[0].endswith("/grpc_cpp_plugin_binary") for v in selected.values())
        report["selected"] = {"wrapper_actions": wrapper_actions, "plugin": selected}
        save()
        assert all(c.startswith("bazel-out/") for vs in selected.values() for c in [vs[0].split("=", 2)[-1]])
        spawns = TEMP / "copy-spawns.json"
        build = [*prior.bazel, "build", *flags, "--execution_log_json_file=" + str(spawns), *targets]
        run("generate", build, ROOT)
        accesses = []
        copies = []
        for s in prior.spawns(spawns):
            owner = prior.label(s.get("targetLabel", ""))
            if owner == prior.wrapper and s.get("mnemonic") == "CopyGrpcPlugin":
                argv = s["commandArgs"]
                props = {p["name"]: p["value"] for p in s["platform"]["properties"]}
                inputs = {f["path"]: f.get("digest", {}) for f in s.get("inputs", [])}
                outputs = {f["path"]: f.get("digest", {}) for f in s.get("actualOutputs", [])}
                copies.append({"argv": argv, "image": props.get("container-image"), "runner": s["runner"],
                               "cached": s.get("cacheHit", False) or s.get("remoteCacheHit", False),
                               "exit": s.get("exitCode", 0), "tool": inputs.get(argv[0]), "source": inputs.get(argv[1]),
                               "result": outputs.get(argv[2])})
            if owner in targets and s.get("mnemonic") == "Action":
                props = {p["name"]: p["value"] for p in s["platform"]["properties"]}
                accesses.append({"owner": owner, "runner": s["runner"], "exit": s["exitCode"],
                                 "cached": s.get("cacheHit", False) or s.get("remoteCacheHit", False),
                                 "image": props.get("container-image"), "isolation": props.get("workload-isolation-type"),
                                 "plugin": [v for v in s["commandArgs"] if v.startswith("--plugin=")],
                                 "plugin_digest": next((i.get("digest") for i in s.get("inputs", [])
                                                       if i["path"] == selected[owner][0].split("=", 2)[-1]), None)})
        report["copies"] = copies
        report["executions"] = accesses
        expected = json.loads((ROOT / "test/rbe/grpc-native-codegen/result.json").read_bytes())["matches"]
        actual = {p: {"size": (ROOT / "bazel-bin" / p).stat().st_size, "sha256": digest(ROOT / "bazel-bin" / p)} for p in expected}
        report["outputs"] = actual
        save()
        assert sorted(a["owner"] for a in accesses) == sorted(targets)
        assert all(a["runner"] == "remote" and not a["exit"] and not a["cached"]
                   and a["image"] == "docker://" + os.environ["MINIMAL"] and a["isolation"] == "oci"
                   and a["plugin"] == selected[a["owner"]] for a in accesses)
        assert len(copies) == 1
        c = copies[0]
        assert len(c["argv"]) == 3 and c["argv"][0].endswith("/bazel/copy_file")
        assert c["argv"][1].endswith("/grpc_cpp_plugin_native") and c["argv"][2].endswith("/grpc_cpp_plugin_binary")
        assert c["runner"] == "remote" and not c["cached"] and c["exit"] == 0 and c["image"] == "docker://" + os.environ["MINIMAL"]
        assert c["source"] and c["source"]["hash"] == c["result"]["hash"] and c["tool"]["hash"]
        assert all(a["plugin_digest"]["hash"] == c["result"]["hash"] for a in accesses)
        assert actual == expected
    finally:
        module.write_bytes(old)


def metadata():
    import yaml

    # Compare the canonical Python manifest to the original complete upstream receipt.
    original = "43fc5ed07ec209caa11eaea6813b0f6e3485f796cc51c465a346a1edeff7bb8d"
    manifest = SUPPLIER / "build_autogenerated.yaml"
    run("python-manifest", ["python3", "tools/buildgen/extract_metadata_from_bazel_xml.py"])
    report["python_manifest"] = digest(manifest)
    expected = {t["name"] for t in yaml.safe_load(manifest.read_bytes())["targets"]
                if t["name"].startswith("grpc_") and t["name"].endswith("_plugin")}
    save()
    assert report["python_manifest"] == original and len(expected) == 7
    run("cmake-inputs", ["git", "submodule", "update", "--init", "--depth=1", "--", "third_party/boringssl-with-bazel",
                         "third_party/benchmark", "third_party/cares/cares"])
    # Use the owning renderer, its normal additional build inputs, and only the CMake template.
    run("render-cmake", ["bash", "-ec", "source tools/buildgen/generate_build_additions.sh\n"
        "python3 tools/buildgen/generate_projects.py build_handwritten.yaml build_autogenerated.yaml "
        "$gen_build_files --templates templates/CMakeLists.txt.template --jobs 2"])
    rendered = (SUPPLIER / "CMakeLists.txt").read_bytes()
    checked = subprocess.check_output(["git", "show", BASE + ":CMakeLists.txt"], cwd=SUPPLIER)
    def stanzas(cmake):
        return {name: re.search(rb"add_executable\(" + name.encode() + rb"\s+[^)]*\)", cmake)[0].decode()
                for name in sorted(expected)}
    report["cmake"] = {"rendered": hashlib.sha256(rendered).hexdigest(), "tracked": hashlib.sha256(checked).hexdigest(),
                       "plugins": stanzas(rendered)}
    save()
    assert stanzas(checked) == stanzas(rendered)
    run("restore-cmake", ["git", "restore", "--", "CMakeLists.txt"])
    # artifact_gen writes generated files into its checkout. Restore only those
    # generated by this run before switching between the two source versions.
    for name, ref in [("base", BASE), ("candidate", SOURCE)]:
        run("select-" + name, ["git", "checkout", "--detach", ref])
        assert output("git", "rev-parse", "HEAD") == ref
        run("artifact-gen-" + name, ["tools/artifact_gen/artifact_gen.sh"])
        model = json.loads((SUPPLIER / "tools/artifact_gen/build.json").read_bytes())
        plugins = {t["name"]: t for t in model["targets"] if t["name"] in expected}
        changes = output("git", "diff", "--name-only").splitlines()
        generated = [n for n in changes if (SUPPLIER / n).is_file()]
        report[name] = {"plugins": plugins, "generated": {n: digest(SUPPLIER / n) for n in generated}}
        save()
        if changes:
            run("restore-generated-" + name, ["git", "restore", "--", *changes])
    assert report["python_manifest"] == original and len(expected) == 7
    assert report["base"] == report["candidate"]
    assert set(report["candidate"]["plugins"]) == expected
    assert all(t["src"] for t in report["candidate"]["plugins"].values())


def darwin():
    assert platform.system() == "Darwin"
    bazel = [str(SUPPLIER / "tools/bazel")]
    names = ["grpc_cpp_plugin", "grpc_objective_c_plugin"]
    targets = ["//src/compiler:" + n for n in names]
    proto = "@com_google_protobuf//:protoc"
    flags = ["--jobs=4", "--copt=-g0", "--host_copt=-g0"]
    sources = []
    for arm, ref in [("base", BASE), ("candidate", SOURCE)]:
        run("select-" + arm, ["git", "checkout", "--detach", ref])
        assert output("git", "rev-parse", "HEAD") == ref
        run("build-" + arm, [*bazel, "build", *flags, *targets, proto])
        root = Path(output(*bazel, "info", "execution_root"))
        protoc = root / output(*bazel, "cquery", *flags, "--output=files", proto)
        dist = TEMP / ("dist-" + arm)
        dist.mkdir()
        shutil.copy(protoc, dist / "protoc")
        shutil.copy(SUPPLIER / "examples/protos/helloworld.proto", dist)
        report[arm] = {"commit": ref, "plugins": {}}
        for n in names:
            p = SUPPLIER / "bazel-bin/src/compiler" / (n + "_binary")
            src = p.with_name(n + "_universal")
            assert stat.S_ISREG(p.lstat().st_mode) and digest(src) == digest(p)
            sources.append(p.resolve(strict=True))
            archive = dist / (n + ".zip")
            # -y stores symlinks instead of silently dereferencing them.
            run("archive-" + arm + n, ["/usr/bin/zip", "-y", "-j", str(archive), str(p)])
            with zipfile.ZipFile(archive) as z:
                info, = z.infolist()
                assert info.filename == p.name and stat.S_ISREG(info.external_attr >> 16)
            run("install-" + arm + n, ["/usr/bin/unzip", "-q", str(archive)], dist)
            installed = dist / p.name
            arches = sorted(output("/usr/bin/lipo", "-archs", str(installed)).split())
            assert arches == ["arm64", "x86_64"] and not installed.is_symlink() and os.access(installed, os.X_OK)
            assert digest(p) == digest(installed)
            report[arm]["plugins"][n] = {"name": p.name, "regular": True, "archive_sha256": digest(archive),
                                        "installed_sha256": digest(installed), "architectures": arches}
            save()
    run("remove-build-tree", [*bazel, "clean", "--expunge"])
    assert not any(p.exists() for p in sources)
    for arm in ["base", "candidate"]:
        dist = TEMP / ("dist-" + arm)
        for n in names:
            run("use-" + arm + n, [str(dist / "protoc"), "--plugin=protoc-gen-grpc=" + str(dist / (n + "_binary")),
                                    "--grpc_out=.", "helloworld.proto"], dist)
        generated = {p.name: digest(p) for p in dist.iterdir() if p.suffix in (".h", ".m", ".cc") and b"sayhello" in p.read_bytes().lower()}
        report[arm]["generated"] = generated
        save()
        assert sorted(s.lower() for s in generated) == ["helloworld.grpc.pb.cc", "helloworld.grpc.pb.h", "helloworld.pbrpc.h", "helloworld.pbrpc.m"]
    assert report["base"]["generated"] == report["candidate"]["generated"]


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
