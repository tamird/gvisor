"""Qualify the gRPC execution-time plugin on two existing gVisor service targets."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

tmp = Path(os.environ["RUNNER_TEMP"])
records = tmp / "grpc-receipt"
bazel = ["bazelisk", f"--bazelrc={tmp}/grpc.bazelrc"]
services = {
    "//pkg/shim/v1/taskserver:_task_server_cc_grpc_proto_grpc_codegen": "pkg/shim/v1/taskserver/task_server",
    "//test/packetimpact/proto:_posix_server_cc_grpc_proto_grpc_codegen": "test/packetimpact/proto/posix_server",
}
wrapper = "@@grpc+//src/compiler:grpc_cpp_plugin"
base = "aa767e4c43d011121271551fe24da501a56f729d"
options = [
    "--platforms=//tools/bazeldefs:linux_amd64", "--extra_toolchains=@local_config_cc_toolchains//:all", "--jobs=3", "--noremote_accept_cached",
    "--host_cxxopt=-std=c++17", "--cxxopt=-std=c++17", "--nostamp", "--workspace_status_command=", "--remote_download_outputs=toplevel",
    "--host_cxxopt=-O0", "--cxxopt=-O0", "--features=fully_static_link", "--host_features=fully_static_link", "--nostart_end_lib",
    "--features=-supports_start_end_lib", "--host_features=-supports_start_end_lib",
    "--linkopt=-static", "--host_linkopt=-static",
    "--linkopt=-fuse-ld=bfd", "--host_linkopt=-fuse-ld=bfd",
    "--linkopt=-Wl,--no-gc-sections", "--host_linkopt=-Wl,--no-gc-sections",
    "--modify_execution_info=^Action$=+no-remote-cache",
    "--modify_execution_info=^Genrule$=+no-remote-cache",
]
source_hashes = {"old": "818441ccf239979a820926bd517ba9a2fa9b77e805943baa411edc5e5b3bd4de",
                 "native": "6d4b18be32f4e20ec268877d55a2259551f9617dd64f911976da69b34a35f38c"}
host_module = b'\nhost_cc = use_extension("@rules_cc//cc:extensions.bzl", "cc_configure_extension")\nuse_repo(host_cc, "local_config_cc_toolchains")\n'
sha = lambda content: hashlib.sha256(content).hexdigest()
def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def write(name, data):
    (records / (name + ".json")).write_text(json.dumps(data, indent=2) + "\n")

def spawns(path):
    decoder = json.JSONDecoder()
    with path.open() as stream:
        unused = ""
        while True:
            chunk = stream.read(131072)
            unused += chunk
            while unused.lstrip():
                unused = unused.lstrip()
                try:
                    event, end = decoder.raw_decode(unused)
                except ValueError:
                    if not chunk:
                        raise
                    break
                yield event
                unused = unused[end:]
            if not chunk:
                break

def execute(name, command, stdout=None):
    log = tmp / (name + ".log")
    start = now()
    with log.open("w") as out:
        process = subprocess.run(command, stdout=stdout or out, stderr=out)
    raw = log.read_text(errors="replace")
    # No environment, rc file, raw argv outside of the two explicitly selected action types, or unfiltered setup logs.
    def scrub(line):
        return re.sub(r'(?i)(?:key|authorization|token)[=:][^\s]+', 'REDACTED', line)[:1800]
    def diagnostic(line):
        lowered = line.lower()
        return ("invocation id:" in lowered or line.startswith("ERROR:") or
                any(word in lowered for word in
                    ("error:", "failed to", "cannot ", "undefined reference", "relocation ",
                     "multiple definition", "no such file", "could not", "collect2:", "/usr/bin/ld",
                     "/bin/bash", "killed", "out of memory", "unrecognized option", "not found")))
    rows = [scrub(line) for line in raw.splitlines() if diagnostic(line)]
    data = dict(start=start, finish=now(), exit=process.returncode, evidence=rows[:70])
    write(name+"-run", data)
    print(name, process.returncode, rows[:3], flush=True)
    return data

def label(raw):
    if raw.startswith("@@//") or raw.startswith("@//"):
        return raw.lstrip("@")
    return "@@" + raw.lstrip("@") if raw.startswith("@") else raw

def require_host_link_plan(flags, targets):
    query = 'mnemonic("CppLink", deps(set(' + " ".join(targets) + ')))'
    path = tmp / "link-plan.json"
    with path.open("w") as out:
        rc = execute("native-link-plan", bazel+["aquery",*flags,"--include_param_files","--output=jsonproto",query],out)
    assert not rc["exit"], "could not inspect configured compiler actions"
    graph = json.loads(path.read_bytes())
    names = {t["id"]:label(t["label"]) for t in graph["targets"]}
    expected = {"@@grpc+//src/compiler:grpc_cpp_plugin_native", "@@protobuf+//:protoc",
                "@@protobuf+//src/google/protobuf/compiler:protoc_minimal",
                "@@protobuf+//upb_generator/minitable:protoc-gen-upb_minitable_stage0"}
    rows = []
    for action in graph["actions"]:
        owner = names[action["targetId"]]
        if owner not in expected:
            continue
        argv = action["arguments"]
        params = [v for f in action.get("paramFiles",[]) for v in f["arguments"]]
        args = [*argv,*params]
        rows.append(dict(owner=owner, executable=argv[0], worker=action["executionPlatform"],
                         flags=[v for v in args if v=="-static" or "fuse-ld" in v or "gc-sections" in v or "start-lib" in v], archives=sum(v.endswith(".a") for v in args)))
    write("four-link-actions",rows)
    assert {r["owner"] for r in rows}==expected, rows
    for r in rows:
        f=r["flags"]
        assert r["executable"]=="/usr/bin/gcc",r
        assert "-static" in f and [v for v in f if v.startswith("-fuse-ld=")][-1]=="-fuse-ld=bfd",r
        assert [v for v in f if "gc-sections" in v][-1]=="-Wl,--no-gc-sections",r
        assert r["archives"] and not any("start-lib" in v for v in f),r

def lane(name):
    bare = name != "ordinary"
    native = name == "native"
    flags = options.copy()
    if bare:
        flags.append("--experimental_add_exec_constraints_to_targets=^.*(src/compiler:grpc_cpp_plugin|_grpc_codegen)$=@grpc_workers//:empty")
    targets = list(services)
    query = 'mnemonic("Action|Genrule", deps(set(' + " ".join(targets) + ')))'
    path = tmp / (name+"-graph.json")
    with path.open("w") as target:
        run = execute(name+"-plan", bazel+["aquery",*flags,"--output=jsonproto","--noinclude_artifacts",query],target)
    assert run["exit"] == 0, f"{name}: analysis failed"
    graph = json.loads(path.read_bytes())
    target_names = {t["id"]: label(t["label"]) for t in graph["targets"]}
    interest = [{k: a.get(k) for k in ("mnemonic","executionPlatform","arguments")} | {"owner":target_names[a["targetId"]]}
                for a in graph["actions"] if target_names[a["targetId"]] in [*targets,wrapper]]
    write(name+"-selected-actions",interest)
    shell = [a for a in interest if a["owner"] == wrapper and a["mnemonic"]=="Genrule"]
    generated = [a for a in interest if a["owner"] in targets]
    assert len(shell)==int(not native) and len(generated)==2, (name,interest)
    for a in [*shell,*generated]:
        # Genrule's exec transition discards the root-only target filter. Its
        # qualification-only exec_properties set the reported remote worker.
        expected = "scratch" if bare and a["owner"] != wrapper else "gnu"
        assert a["executionPlatform"].endswith(":"+expected), (name,a)
    for a in generated:
        assert a["mnemonic"]=="Action" and any(x.startswith("--plugin=") and x.endswith("grpc_cpp_plugin_"+("native" if native else "binary")) for x in a["arguments"]),a
    output_base = subprocess.check_output(bazel+["info","output_base"],text=True).strip()
    grpc_source = (Path(output_base)/"external/grpc+/bazel/cc_grpc_library.bzl").read_bytes()
    digest = sha(grpc_source)
    assert digest == source_hashes["native" if native else "old"],digest
    if native:
        require_host_link_plan(flags, targets)
    events = tmp / (name+"-events.json")
    outcome = execute(name, bazel+["build", *flags, "--execution_log_json_file="+str(events), *targets])
    selected = []
    companion_links = []
    if events.is_file():
        for s in spawns(events):
            owner = label(s.get("targetLabel",""))
            if s.get("mnemonic")=="CppLink" and owner in ("@@grpc+//src/compiler:grpc_cpp_plugin_native","@@protobuf+//:protoc"):
                companion_links.append({k:s.get(k) for k in ("targetLabel","runner","cacheHit","exitCode","status","listedOutputs")})
            if owner in [*targets,wrapper] and s.get("mnemonic") in ["Genrule","Action"]:
                selected.append({k:s.get(k) for k in ("targetLabel","mnemonic","commandArgs","platform","runner","cacheHit","remoteCacheHit","exitCode","status","listedOutputs","actualOutputs")})
    write(name+"-executions",selected)
    write(name+"-hosted-links",companion_links)
    hashes = {}
    if outcome["exit"]==0:
        paths = [f"{stem}.grpc.pb.{ext}" for stem in services.values() for ext in ["h","cc"]]
        for path in paths:
            contents = (Path("bazel-bin") / path).read_bytes()
            assert contents and b"grpc" in contents,path
            hashes[path]={"size":len(contents),"sha256":sha(contents)}
    write(name+"-outputs",hashes)
    if native and outcome["exit"] == 0:
        # The real generator executed on an empty rootfs. Keep independent ELF
        # interpreter/dynamic dependency properties of both actual command paths.
        root = Path(subprocess.check_output(bazel+["info","execution_root"],text=True).strip())
        paths = [generated[0]["arguments"][0], next(v.split("=",2)[2] for v in generated[0]["arguments"] if v.startswith("--plugin="))]
        info = {}
        for path in paths:
            tool = root / path
            if not tool.is_file():
                info[path] = {"elf_inspection":"missing local tool output", "ran_on":"scratch, see original execution"}
                continue
            library = subprocess.check_output(["readelf","-dW",tool],text=True)
            segments = subprocess.check_output(["readelf","-lW",tool],text=True)
            info[path] = dict(sha256=sha(tool.read_bytes()), contains_interp="INTERP" in segments, contains_needed="(NEEDED)" in library)
            assert not info[path]["contains_interp"] and not info[path]["contains_needed"], info[path]
        write("native-executables", info)
    if name == "stock-scratch":
        assert outcome["exit"] and any("grpc_cpp_plugin" in t for t in outcome["evidence"]),outcome
        assert any("/bin/bash" in t and any(v in t.lower() for v in ["not found","no such file","could not start","failed to start"]) for t in outcome["evidence"]),outcome
        copies = [a for a in selected if a["mnemonic"]=="Genrule" and label(a["targetLabel"])==wrapper]
        assert len(copies)==1, copies
        cp=copies[0]
        props={p["name"]:p["value"] for p in cp["platform"]["properties"]}
        assert props.get("container-image")=="docker://"+os.environ["MINIMAL"],cp
        assert cp["runner"]=="remote" and (cp["exitCode"] or cp["status"]),cp
        assert shell[0]["arguments"][0].endswith("/bash") and "cp " in " ".join(shell[0]["arguments"]),shell
    else:
        assert outcome["exit"]==0,outcome
        generated_spawns = [a for a in selected if a["mnemonic"]=="Action" and label(a["targetLabel"]) in targets]
        assert sorted(label(a["targetLabel"]) for a in generated_spawns)==sorted(targets), generated_spawns
        for a in generated_spawns:
            props={p["name"]:p["value"] for p in a["platform"]["properties"]}
            assert props["container-image"]=="docker://"+os.environ["MINIMAL" if bare else "GNU"],a
            assert a["runner"]=="remote" and not a["cacheHit"] and not a["remoteCacheHit"] and not a["exitCode"],a
    return dict(source_file_sha256=digest, outputs=hashes,original_exit=outcome["exit"])

def main():
    patch=Path("test/rbe/grpc-native-codegen/native.patch")
    result=dict(gvisor_base=base,grpc_release="v1.84.0",grpc_patch_sha256=sha(patch.read_bytes()),start=now(),targets=list(services),lanes={},errors={})
    original = Path("MODULE.bazel").read_bytes()
    assert subprocess.check_output(["git","show",base+":MODULE.bazel"])==original
    try:
        for name in ["native","ordinary","stock-scratch"]:
            Path("MODULE.bazel").write_bytes(original + host_module)
            if name=="stock-scratch":
                with Path("MODULE.bazel").open("ab") as m:
                    m.write(b'\nsingle_version_override(module_name="grpc", patches=["//test/rbe/grpc-native-codegen:stock-worker.patch"], patch_strip=1)\n')
            if name=="native":
                with Path("MODULE.bazel").open("ab") as m:
                    m.write(b'\nsingle_version_override(module_name="grpc", patches=["//test/rbe/grpc-native-codegen:native.patch"], patch_strip=1)\n')
            try:
                result["lanes"][name]=lane(name)
            except Exception as exc:
                result["errors"][name]=str(exc)[:3600]
                if name=="native":
                    result["unexecuted"]=["ordinary","stock-scratch"]
                    break
            write("progress",result)
        if not result["errors"]:
            assert result["lanes"]["ordinary"]["outputs"]==result["lanes"]["native"]["outputs"]
    finally:
        Path("MODULE.bazel").write_bytes(original)
        result["finish"]=now()
        result["status"]="PASS" if not result["errors"] and len(result["lanes"])==3 and result["lanes"]["ordinary"]["outputs"]==result["lanes"]["native"]["outputs"] else "FAIL"
        write("verdict",result)
        print("Verdict",result["status"],result["errors"],flush=True)
    return int(result["status"]!="PASS")
if __name__=="__main__":
    raise SystemExit(main())
