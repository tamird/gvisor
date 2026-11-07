"""Exercise the registry c-ares selected by the actual gVisor packetimpact consumer."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

BASE = "fb9ee22abe02f61549755018afd2c124e55eea98"
FIX = "ca5436ed33fd2bc67dba510a4ecf730d972448fc"
VERSION = "1.34.9.bcr.1"
DUT = "//test/packetimpact/dut:posix_server"
GRPC = "@com_github_grpc_grpc//:grpc++"
CARES = "@@c-ares+//:ares"
HEADER = "@@c-ares+//:ares_config_h"
SOURCE = "@@c-ares+//:include/ares_build.h"
TEST = "//test/packetimpact/cares_qualification:server_test"
W = Path(os.environ["RUNNER_TEMP"]) / "cares-consumer"
OUT = W / "out"
STORE = OUT / "consumer.json"
RC = ["--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g", "--bazelrc=" + str(W / "bounds.bazelrc")]
BZ = [str(W / "bin/bazel"), *RC]
LICENSE = Path("tools/licensecheck/dependencies.yaml")
MARKER = "Verified POSIX Socket and Close against the compiled C++ gRPC server"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save():
    STORE.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def call(name, args, check=True):
    out, err = W / (name + ".stdout"), W / (name + ".stderr")
    with out.open("wb") as stdout, err.open("wb") as stderr:
        rc = subprocess.run(args, stdout=stdout, stderr=stderr).returncode
    row = {"exit": rc}
    if rc:
        lines = err.read_text(errors="replace").splitlines() + out.read_text(errors="replace").splitlines()
        relevant = [s for s in lines if "ERROR" in s or "licensecheck:" in s or "missing entry" in s or "no such" in s]
        row["diagnostic"] = [re.sub(r"https?://[^\s)]+", "<public download>", s)[:320]
                             for s in (relevant[:6] or lines[-4:]) if not re.search(r"(?i)token=|authorization:", s)]
    report["calls"][name] = row
    save()
    if rc and check:
        raise ValueError(f"{name} exited {rc}")
    return rc, out


def output(name, *args):
    return call(name, [*BZ, *args])[1].read_text()


def version(stage):
    graph = json.loads(output(stage + "-graph", "mod", "graph", "--depth=3", "--output=json"))
    def walk(node):
        if isinstance(node, list):
            for item in node:
                yield from walk(item)
        if isinstance(node, dict):
            if isinstance(node.get("key"), str):
                yield node["key"]
            for item in node.values():
                yield from walk(item)
    keys = set(walk(graph))
    grpc, = sorted(k.removeprefix("grpc@") for k in keys if k.startswith("grpc@"))
    cares, = sorted(k.removeprefix("c-ares@") for k in keys if k.startswith("c-ares@"))
    assert grpc == "1.84.0", grpc
    report[stage] = {"grpc": grpc, "cares": cares, "root_visible_graph": [d["key"] for d in graph.get("dependencies", []) if d.get("name") == "c-ares"]}
    # The C++ binary directly depends on gRPC; require that the latter reaches the selected c-ares library.
    direct = output(stage + "-direct", "cquery", "--config=x86_64", "deps(" + DUT + ", 1) intersect " + GRPC)
    path = output(stage + "-grpc-cares", "cquery", "--config=x86_64", "somepath(" + GRPC + ", " + CARES + ")")
    assert "//:grpc++ (" in direct and "//:grpc++ (" in path and "//:ares (" in path
    report[stage]["consumer"] = DUT
    report[stage]["grpc_to_cares"] = path.splitlines()
    save()
    return cares


def path_table(graph):
    fragments = {str(p["id"]): p for p in graph.get("pathFragments", [])}
    def path(key):
        f = fragments[str(key)]
        return (path(f["parentId"]) + "/" if f.get("parentId") else "") + f["label"]
    return {str(a["id"]): path(a["pathFragmentId"]) for a in graph.get("artifacts", [])}


def header(stage, arch):
    flags = ["--config=" + arch]
    name = stage + "-" + arch
    # Scope the action to an actual transitive dependency of the shipping binary.
    expression = 'outputs("(^|/)ares_(config|build)[.]h$", deps(' + DUT + '))'
    graph = json.loads(output(name + "-actions", "aquery", *flags, "--output=jsonproto",
                              "--include_artifacts", "--noinclude_commandline", expression))
    paths = path_table(graph)
    owners = {str(t["id"]): t["label"] for t in graph.get("targets", [])}
    configs = {str(c["id"]): c for c in graph.get("configuration", [])}
    deps = {str(d["id"]): d for d in graph.get("depSetOfFiles", [])}
    def inputs(ids):
        done, result = set(), set()
        todo = [str(x) for x in ids]
        while todo:
            k = todo.pop()
            if k in done:
                continue
            done.add(k)
            d = deps[k]
            result.update(paths[str(a)] for a in d.get("directArtifactIds", []))
            todo.extend(str(a) for a in d.get("transitiveDepSetIds", []))
        return sorted(result)
    actions = []
    for a in graph.get("actions", []):
        owner = owners[str(a["targetId"])]
        if "c-ares+" not in owner:
            continue
        actions.append({"label": owner, "mnemonic": a["mnemonic"], "config": configs[str(a["configurationId"])]["checksum"],
                        "inputs": [s for s in inputs(a.get("inputDepSetIds", [])) if s.endswith(("/ares_build.h", "/ares_config.h"))],
                        "outputs": [paths[str(i)] for i in a["outputIds"]]})
    report[stage].setdefault("headers", {})[arch] = {"actions": actions}
    save()
    if stage == "base":
        assert actions, "missing original c-ares producer"
        return
    live = [a for a in actions if a["label"].endswith("//:ares_config_h")]
    assert live and all(a["mnemonic"] == "Symlink" and len(a["inputs"]) == len(a["outputs"]) == 1
                        and a["inputs"][0].endswith("/include/config_linux/ares_config.h") for a in live), actions
    source_path = output(name + "-source", "cquery", *flags, "somepath(" + CARES + ", " + SOURCE + ")")
    assert "//:include/ares_build.h (" in source_path, source_path
    # Materialize these specific header outputs before switching target CPU.
    bep = W / (name + ".bep")
    output(name + "-build", "build", *flags, "--build_event_publish_all_actions",
           "--build_event_json_file=" + str(bep), HEADER)
    root = Path(output(name + "-execution-root", "info", "execution_root").strip())
    original = [json.loads(line) for line in bep.open()]
    cfg, = [e["id"]["targetCompleted"]["configuration"]["id"] for e in original
            if e.get("completed", {}).get("success") and "targetCompleted" in e["id"]
            and not e["id"]["targetCompleted"].get("aspect")
            and e["id"]["targetCompleted"]["label"].endswith("//:ares_config_h")]
    precise = "config(" + HEADER + ", " + cfg + ")"
    cpu, platform = ("k8", "linux_amd64") if arch == "x86_64" else ("aarch64", "linux_arm64")
    opts = output(name + "-platform", "cquery", *flags, "--output=starlark",
                  '--starlark:expr=str(build_options(target)["//command_line_option:cpu"]) + "|" + str(build_options(target)["//command_line_option:platforms"])', precise).strip()
    assert opts.startswith(cpu + "|") and ":" + platform in opts, opts
    generated_paths = output(name + "-files", "cquery", *flags, "--output=files", precise).splitlines()
    generated, = generated_paths
    row, = [a for a in live if a["config"] == cfg and generated in a["outputs"]]
    produced = root / generated
    selected = root / row["inputs"][0]
    build_source = next(p for p in selected.parents if p.name == "c-ares+") / "include/ares_build.h"
    assert produced.is_symlink() and produced.samefile(selected)
    assert sha(produced.read_bytes()) == "8d30eb7faf4f0b819153c476f01669a72754548e121659c73928378d12d37ea2"
    assert sha(build_source.read_bytes()) == "3f837fc8a61881fa6a4ec2be50f475cd833f60fbd27f52688a6f733669ccda8e"
    report[stage]["headers"][arch].update({"materialized_config":row["config"], "target_cpu_platform":opts, "selected": row["inputs"][0],
        "output": generated, "symlink": True, "ares_config_sha256":sha(produced.read_bytes()),
        "ares_build_source_sha256":sha(build_source.read_bytes())})
    save()


def licenses():
    command = ["make", "--no-print-directory", "DOCKER_BUILD=false", "STARTUP_OPTIONS=" + shlex.join(RC)]
    before = LICENSE.read_bytes()
    rc, out = call("license-verify", [*command, "license-check"], False)
    if rc:
        diagnostics = (W / "license-verify.stderr").read_text(errors="replace") + out.read_text(errors="replace")
        assert "missing entry for c-ares" in diagnostics, "license failure is not the missing candidate record"
        call("license-generate", [*command, "run", "TARGETS=//tools/licensecheck/main:licensecheck", "ARGS=--mode=fetch"])
        blocks = re.split(rb"(?m)(?=^- dependency: )", LICENSE.read_bytes())
        entry, = [block for block in blocks if block.splitlines()[0] == b"- dependency: c-ares"]
        assert len(entry) <= 8192 and VERSION.encode() in entry
        old = re.split(rb"(?m)(?=^- dependency: )", before)
        def key(block):
            return block.splitlines()[0].removeprefix(b"- dependency: ")
        LICENSE.write_bytes(old[0] + b"".join(sorted(old[1:] + [entry], key=key)))
        (OUT / "cares-license.yaml").write_bytes(entry)
        call("license-verify-generated", [*command, "license-check"])
    report["licenses"] = {"verified":True,"generated":bool(rc),"before":sha(before),"after":sha(LICENSE.read_bytes())}
    save()


def consume():
    bep = W / "native-consumer.bep"
    output("posix-server-test", "test", "--config=x86_64", "--nocache_test_results", "--test_arg=-test.v",
           "--test_output=errors", "--build_event_json_file=" + str(bep), TEST)
    events = [json.loads(s) for s in bep.open()]
    test, = [e for e in events if "testResult" in e and e["id"]["testResult"]["label"] == TEST]
    result = test["testResult"]
    assert result["status"] == "PASSED" and not result.get("cachedLocally") and not result.get("executionInfo", {}).get("cachedRemotely")
    outputs = {f["name"]: f for f in result["testActionOutput"]}
    def content(name):
        f = outputs[name]
        uri = urlparse(f["uri"])
        assert uri.scheme == "file" and not uri.netloc, uri
        p = Path(unquote(uri.path))
        data = p.read_bytes()
        assert not f.get("digest") or f["digest"] == sha(data)
        assert int(f.get("length",len(data))) == len(data)
        return data
    log, xml = content("test.log"), content("test.xml")
    assert MARKER.encode() in log
    leaves = [n for n in ET.fromstring(xml).iter("testcase") if n.get("name") == "TestPOSIXSocketAndClose"]
    assert len(leaves) == 1 and all(leaves[0].find(tag) is None for tag in ("skipped","failure","error"))
    assert leaves[0].get("status") not in ("notrun", "skipped") and leaves[0].get("result") not in ("suppressed", "skipped")
    root = Path(output("server-execution-root","info","execution_root").strip())
    path, = output("server-default","cquery","--config=x86_64","--output=files",DUT).splitlines()
    compiled = (root/path).read_bytes()
    assert (MARKER + " sha256=" + sha(compiled)).encode() in log
    assert compiled[:7] == b"\x7fELF\x02\x01\x01" and int.from_bytes(compiled[18:20],"little") == 62
    report["native_rpc"] = {"test":TEST,"case":"TestPOSIXSocketAndClose","server":DUT,"server_sha256":sha(compiled),
                            "status":result["status"], "runner":result.get("executionInfo",{}).get("strategy"),
                            "log_sha256":sha(log),"xml_sha256":sha(xml)}
    save()


def main(stage):
    actual = subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()
    assert actual == (BASE if stage == "base" else FIX)
    v = version(stage)
    if stage == "base":
        assert v != VERSION, "stock already resolves the corrected c-ares"
        header(stage,"x86_64")
        return
    assert v == VERSION and report["base"]["cares"] != v
    # Keep independent headers, license and ordinary native RPC evidence on failure.
    for name, fn in (("amd64_header",lambda:header(stage,"x86_64")),("arm64_header",lambda:header(stage,"aarch64")),
                     ("licenses",licenses),("native_rpc",consume)):
        try:
            fn()
        except Exception as e:
            report.setdefault("failures", {})[name] = str(e)[:450]
            save()
    assert not report.get("failures"), report["failures"]
    report["complete"] = True


if __name__ == "__main__":
    import sys
    report = json.loads(STORE.read_bytes()) if STORE.exists() else {"source":FIX,"parent":BASE,"calls":{},"complete":False}
    try:
        main(sys.argv[1])
    except Exception as e:
        report["failure"] = type(e).__name__ + " " + str(e)[:450]
        raise
    finally:
        save()
