"""Request the old parser's RDFGraph object with the current gVisor compiler."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

out = Path(os.environ["RUNNER_TEMP"]) / "rdf-graph-results"
out.mkdir()
common = ["--config=x86_64", "--extra_toolchains=@llvm//toolchain:all", "--lockfile_mode=off", "--noshow_progress", "--color=no"]
binary = "//tools/nvidia_driver_differ:driver_ast_parser"
probe = "//.github/qualification/rdf_graph:object"
pattern = 'outputs(".*(RDFGraph|RDFCopy|HexagonRDFOpt)[.](pic[.])?o", mnemonic("CppCompile", deps(%s)))'

def bazel(name, command, *args):
    argv = [os.environ["BAZEL"], "--nosystem_rc", "--nohome_rc", command, *common, *args]
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (out / (name + ".log")).write_bytes(result.stderr[-45000:])
    return result

def actions(name, target):
    run = bazel(name, "aquery", "--noinclude_artifacts", "--output=jsonproto", pattern % target)
    run.check_returncode()
    data = json.loads(run.stdout)
    targets = {t["id"]: t["label"] for t in data["targets"]}
    result = []
    for a in data["actions"]:
        argv = a["arguments"]
        src = argv[argv.index("-c") + 1]
        assert src.endswith("/llvm/lib/CodeGen/RDFGraph.cpp"), src
        assert targets[a["targetId"]].endswith("llvm-project//llvm:CodeGen"), targets[a["targetId"]]
        assert "llvm-toolchain-minimal-linux-amd64/bin/clang++" in argv[0], argv[0]
        result.append({"owner": targets[a["targetId"]], "argv": argv, "object": argv[argv.index("-o")+1]})
    assert result
    (out / (name + ".json")).write_text(json.dumps(result, indent=2) + "\n")
    return result

parser = actions("parser", binary)
selected = actions("projection", probe)
paths = bazel("outputs", "cquery", "--output=files", probe)
paths.check_returncode()
objects = set(paths.stdout.decode().splitlines())
parser_actions = {r["object"]: r["argv"] for r in parser}
projected = {r["object"]: r["argv"] for r in selected if r["object"] in objects}
assert objects and set(projected) == objects
assert all(parser_actions[p] == projected[p] for p in objects)
run = bazel("compile", "build", "--verbose_failures", "--build_event_json_file=" + str(out / "events.jsonl"), probe)
err = run.stderr.decode(errors="replace")
diags = [v for v in err.splitlines() if ("error:" in v or "RDFGraph.cpp:" in v)][:30]
info = {"raw_llvm_revision": "cb2f0d0a5f14c183e7182aba0f0e54a518de9e3f",
        "parser_backends": ["AArch64", "X86"],
        "source": subprocess.check_output(["git","rev-parse","HEAD^"], text=True).strip(),
        "head": subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),
        "outputs": sorted(objects), "compiler": parser[0]["argv"][0], "compile_exit": run.returncode,
        "diagnostics": diags}
if run.returncode:
    info["same_error"] = "llvm::rdf::RegisterRef" in err and "make_transparent.h:" in err
else:
    info["objects"] = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in objects}
(out / "result.json").write_text(json.dumps(info, indent=2) + "\n")
print(json.dumps(info))
assert not run.returncode or info["same_error"], diags
