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

SOURCE = "8e12f9a3e2460d4b1a5b14fb2ef0bf91e86ff4e5"
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
    module.write_bytes(old + prior.host_module + ('\nlocal_path_override(module_name="grpc",path="' + str(SUPPLIER) + '")\n').encode())
    try:
        graph_path = TEMP / "copy-plan.json"
        query = 'mnemonic("Action|Genrule|ExecutableSymlink", deps(set(' + " ".join(targets) + ')))'
        with graph_path.open("wb") as out:
            run("plan", [*prior.bazel, "aquery", *prior.options, "--output=jsonproto", "--noinclude_artifacts", query], ROOT, out)
        graph = json.loads(graph_path.read_bytes())
        labels = {t["id"]: prior.label(t["label"]) for t in graph["targets"]}
        actions = [(labels[a["targetId"]], a) for a in graph["actions"]]
        wrapper_actions = [a["mnemonic"] for owner, a in actions if owner == prior.wrapper]
        report["wrapper"] = wrapper_actions
        save()
        assert wrapper_actions == ["ExecutableSymlink"], wrapper_actions
        selected = {owner: [v for v in a["arguments"] if v.startswith("--plugin=")]
                    for owner, a in actions if owner in targets and a["mnemonic"] == "Action"}
        assert set(selected) == set(targets) and all(len(v) == 1 and v[0].endswith("/grpc_cpp_plugin_binary") for v in selected.values())
        report["selected"] = {"wrapper_actions": wrapper_actions, "plugin": selected}
        save()
        assert all(c.startswith("bazel-out/") for vs in selected.values() for c in [vs[0].split("=", 2)[-1]])
        spawns = TEMP / "copy-spawns.json"
        build = [*prior.bazel, "build", *prior.options, "--execution_log_json_file=" + str(spawns), *targets]
        failed = None
        try:
            run("generate", build, ROOT)
        except RuntimeError as error:
            failed = error
        accesses = []
        for s in prior.spawns(spawns) if spawns.exists() else []:
            owner = prior.label(s.get("targetLabel", ""))
            if owner in targets and s.get("mnemonic") == "Action":
                props = {p["name"]: p["value"] for p in s["platform"]["properties"]}
                accesses.append({"owner": owner, "runner": s["runner"], "exit": s["exitCode"],
                                 "cached": s.get("cacheHit", False) or s.get("remoteCacheHit", False),
                                 "image": props.get("container-image"), "isolation": props.get("workload-isolation-type"),
                                 "plugin": [v for v in s["commandArgs"] if v.startswith("--plugin=")],
                                 "plugin_digest": next((i.get("digest") for i in s.get("inputs", [])
                                                       if i["path"] == selected[owner][0].split("=", 2)[-1]), None)})
        report["executions"] = accesses
        save()
        if failed:
            raise failed
        expected = json.loads((ROOT / "test/rbe/grpc-native-codegen/result.json").read_bytes())["matches"]
        actual = {p: {"size": (ROOT / "bazel-bin" / p).stat().st_size, "sha256": digest(ROOT / "bazel-bin" / p)} for p in expected}
        report["outputs"] = actual
        save()
        assert sorted(a["owner"] for a in accesses) == sorted(targets)
        assert all(a["runner"] == "remote" and not a["exit"] and not a["cached"]
                   and a["image"] == "docker://" + os.environ["MINIMAL"] and a["isolation"] == "oci"
                   and a["plugin"] == selected[a["owner"]] for a in accesses)
        root = Path(output(*prior.bazel, "info", "execution_root", cwd=ROOT))
        for action in accesses:
            tool = root / action["plugin"][0].split("=", 2)[-1]
            native = tool.with_name("grpc_cpp_plugin_native")
            action["local_symlink"] = tool.is_symlink()
            action["native_sha256"] = digest(native)
            action["public_sha256"] = digest(tool)
            save()
            assert action["public_sha256"] == action["native_sha256"]
            assert action["plugin_digest"]["hash"] == action["native_sha256"]
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
    # Compare the complete model to the retained upstream generator result.
    run("artifact-gen", ["tools/artifact_gen/artifact_gen.sh"])
    model = json.loads((SUPPLIER / "tools/artifact_gen/build.json").read_bytes())
    plugin_names = {"src/compiler:" + n for n in expected}
    plugins = {t["name"]: t for t in model["targets"] if t["name"] in plugin_names}
    changes = output("git", "diff", "--name-only").splitlines()
    generated = [n for n in changes if (SUPPLIER / n).is_file()]
    report["candidate"] = {"plugins": plugins, "build_json": digest(SUPPLIER / "tools/artifact_gen/build.json"),
                    "actual_plugin_names": [t["name"] for t in model["targets"] if "plugin" in t["name"] and t.get("build") == "protoc"],
                    "generated": {n: digest(SUPPLIER / n) for n in generated}}
    save()
    if changes:
        run("restore-generated", ["git", "restore", "--", *changes])
    assert report["python_manifest"] == original and len(expected) == 7
    assert report["candidate"]["build_json"] == "d2f216d4525680031f09a35a8fe8514c77c30abbb8555e90a5e577e85a432101"
    assert not report["candidate"]["generated"]
    assert digest(SUPPLIER / "CMakeLists.txt") == report["cmake"]["rendered"]
    assert set(report["candidate"]["plugins"]) == plugin_names
    assert all(t["src"] for t in report["candidate"]["plugins"].values())


def darwin():
    assert platform.system() == "Darwin"
    bazel = [str(SUPPLIER / "tools/bazel")]
    names = ["grpc_cpp_plugin", "grpc_objective_c_plugin"]
    targets = ["//src/compiler:" + n for n in names]
    proto = "@com_google_protobuf//:protoc"
    flags = ["--jobs=4", "--copt=-g0", "--host_copt=-g0"]
    sources = []
    report["stock_receipt"] = {
        "url": "https://github.com/tamird/gvisor/actions/runs/41479697892/artifacts/13159469734",
        "zip_sha256": "5b1a6667952a1e0471d1964014658e721f191c55631efadd0ed9b93dbe43951d",
        "commit": BASE,
        "generated": {
            "helloworld.grpc.pb.cc": "738d2ab9a111935900f7993b6014eaf34c38e2980d6d28998d0e1a62bbc16933",
            "helloworld.grpc.pb.h": "6ac93a6f47fbc36da51058b47bb3390069defe1ce6687ba14c6691beb8a04060",
            "Helloworld.pbrpc.m": "b6246275441a2c4bd59989d8527b2c16f50b0a0b20b13667c9afaabb59d1eca5",
            "Helloworld.pbrpc.h": "22be6152957e9746bb132d062379993db27e0cc6c7375b5e53760dfd150cc959"
},
    }
    # Apply only the two package targets from the release maintainer's #38677.
    # The declarations address the unmodified public compiler labels.
    recipe = '''
pkg_zip(
    name = "grpc_objective_c_plugin_zip",
    srcs = [
        "//src/compiler:grpc_objective_c_plugin",
    ],
    out = "grpc_objective_c_plugin-" + version + "-macos-universal.zip",
    target_compatible_with = [
        "@platforms//os:macos",
    ],
)

pkg_zip(
    name = "grpc_cpp_plugin_zip",
    srcs = [
        "//src/compiler:grpc_cpp_plugin",
    ],
    out = "grpc_cpp_plugin-" + version + "-macos-universal.zip",
    target_compatible_with = [
        "@platforms//os:macos",
    ],
)
'''
    assert hashlib.sha256(recipe.encode()).hexdigest() == "e42a994a92447b7fb48a5b7dff4c0c9280ac1185bf0a92da5718af5b2bbca3cd"
    build = SUPPLIER / "BUILD"
    anchor = 'load("@bazel_skylib//lib:selects.bzl", "selects")'
    contents = build.read_text()
    assert contents.count(anchor) == 1
    build.write_text(contents.replace(anchor, anchor + '\nload("@rules_pkg//pkg:zip.bzl", "pkg_zip")') + recipe)
    module = SUPPLIER / "MODULE.bazel"
    with module.open("a") as f:
        f.write('\nbazel_dep(name = "rules_pkg", version = "1.1.0")\n')
    owners = ["//:" + n + "_zip" for n in names]
    report["publisher"] = {"source": "0c725d3908d9de82314025857388c27dfb41b31a", "rules_pkg": "1.1.0", "owners": owners}
    save()
    run("build-candidate-and-publisher-zips", [*bazel, "build", *flags, *targets, proto, *owners])
    output_base = Path(output(*bazel, "info", "output_base"))
    writers = list((output_base / "external").glob("rules_pkg*/pkg/private/zip/build_zip.py"))
    assert len(writers) == 1, [w.parts[-5] for w in writers]
    report["publisher"]["writer_sha256"] = digest(writers[0])
    save()
    assert digest(writers[0]) == "0fe03d93f83e2a5a21c08289a2d409c52ad7a3be76d8ac24103b6bc171b8a7c7"
    root = Path(output(*bazel, "info", "execution_root"))
    protoc = root / output(*bazel, "cquery", *flags, "--output=files", proto)
    dist = TEMP / "plugin-dist"
    dist.mkdir()
    copied_protoc = dist / "protoc"
    shutil.copy(protoc, copied_protoc)
    proto_file = SUPPLIER / "examples/protos/helloworld.proto"
    plugins = {n: SUPPLIER / "bazel-bin/src/compiler" / (n + "_binary") for n in names}

    def generate(phase, tools):
        work = dist / phase
        work.mkdir()
        shutil.copy(proto_file, work)
        for n, tool in tools.items():
            run(phase + "-" + n, [str(copied_protoc), "--plugin=protoc-gen-grpc=" + str(tool),
                                  "--grpc_out=.", "helloworld.proto"], work)
        files = {p.name: digest(p) for p in work.iterdir() if p.name != "helloworld.proto"}
        report[phase] = files
        save()
        assert files == report["stock_receipt"]["generated"]

    generate("live", plugins)
    expected_plugins = {
        "grpc_cpp_plugin": "e5f42a24b4d16851ce9576a6598147736777003722870abced1845595ef09135",
        "grpc_objective_c_plugin": "634310d7bdc30ec126b24157528a9d4e43fe6741a58b8ebb0f5ad920ace5038f",
    }
    report["plugins"] = {}
    installs = {}
    for name, p in plugins.items():
        src = p.with_name(name + "_universal")
        record = {"symlink": p.is_symlink(), "same_universal_file": p.samefile(src),
                  "sha256": digest(p), "slices": sorted(output("/usr/bin/lipo", "-archs", str(p)).split())}
        report["plugins"][name] = record
        save()
        assert record["symlink"] and record["same_universal_file"]
        record["matches_previous_intel_binary"] = record["sha256"] == expected_plugins[name]
        assert record["slices"] == ["arm64", "x86_64"]
        sources.extend((p, p.resolve(strict=True)))
        paths = output(*bazel, "cquery", *flags, "--output=files", "//:" + name + "_zip").splitlines()
        assert len(paths) == 1
        archive = root / paths[0]
        record["archive"] = archive.name
        record["archive_sha256"] = digest(archive)
        assert archive.name.startswith(name + "-") and archive.name.endswith("-macos-universal.zip")
        with zipfile.ZipFile(archive) as z:
            member, = z.infolist()
            kind = member.external_attr >> 16
            record.update(member=member.filename, mode=oct(kind), uncompressed_bytes=member.file_size)
            save()
            assert member.filename == p.name and stat.S_ISREG(kind) and kind & 0o111
        installed = dist / name
        installed.mkdir()
        run("install-" + name, ["/usr/bin/unzip", "-q", str(archive)], installed)
        target = installed / member.filename
        assert not target.is_symlink() and os.access(target, os.X_OK)
        assert digest(target) == record["sha256"]
        installs[name] = target
    run("remove-build-tree", [*bazel, "clean", "--expunge"])
    assert not any(p.exists() for p in sources)
    generate("publisher_installed", installs)


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
