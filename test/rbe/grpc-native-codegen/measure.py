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
bazel = ["docker", "exec", "grpc-compiler", "bazel", f"--bazelrc={tmp}/grpc.bazelrc"]
services = {
    "//pkg/shim/v1/taskserver:_task_server_cc_grpc_proto_grpc_codegen": "pkg/shim/v1/taskserver/task_server",
    "//test/packetimpact/proto:_posix_server_cc_grpc_proto_grpc_codegen": "test/packetimpact/proto/posix_server",
}
wrapper = "@@grpc+//src/compiler:grpc_cpp_plugin"
base = "aa767e4c43d011121271551fe24da501a56f729d"
options = [
    "--config=x86_64", "--jobs=28", "--nostamp", "--workspace_status_command=", "--remote_download_outputs=toplevel",
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
    rows = [scrub(l) for l in raw.splitlines() if
            (l.startswith("ERROR:") or "no such file" in l.lower() or "/bin/bash" in l or 'exec: "/bin/bash"' in l or "Invocation ID:" in l or "fatal error:" in l)]
    data = dict(start=start, finish=now(), exit=process.returncode, evidence=rows[:22])
    write(name+"-run", data)
    print(name, process.returncode, rows[:3], flush=True)
    return data

def label(raw):
    if raw.startswith("@@//") or raw.startswith("@//"):
        return raw.lstrip("@")
    return "@@" + raw.lstrip("@") if raw.startswith("@") else raw

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
    grpc_source = subprocess.check_output(["docker","exec","grpc-compiler","bash","-c",'cat "$(bazel info output_base 2>/dev/null)/external/grpc+/bazel/cc_grpc_library.bzl"'])
    digest = sha(grpc_source)
    assert digest == source_hashes["native" if native else "old"],digest
    events = tmp / (name+"-events.json")
    outcome = execute(name, bazel+["build", *flags, "--execution_log_json_file="+str(events), *targets])
    if outcome["exit"] and any("killed signal terminated program cc1plus" in t.lower() for t in outcome["evidence"]):
        # Retain the remote compilation failure. The identical coordinator image
        # has a separate memory budget, as in the qualified protobuf fallback.
        events = tmp / (name+"-fallback-events.json")
        outcome = execute(name+"-compile-fallback", bazel+["build",*flags,"--jobs=3","--strategy=CppCompile=local","--noremote_upload_local_results","--execution_log_json_file="+str(events),*targets])
    selected = []
    if events.is_file():
        for s in spawns(events):
            owner = label(s.get("targetLabel",""))
            if owner in [*targets,wrapper] and s.get("mnemonic") in ["Genrule","Action"]:
                selected.append({k:s.get(k) for k in ("targetLabel","mnemonic","commandArgs","platform","runner","cacheHit","remoteCacheHit","exitCode","status","listedOutputs","actualOutputs")})
    write(name+"-executions",selected)
    hashes = {}
    if outcome["exit"]==0:
        paths = [f"{stem}.grpc.pb.{ext}" for stem in services.values() for ext in ["h","cc"]]
        for path in paths:
            contents = subprocess.check_output(["docker","exec","grpc-compiler","cat","bazel-bin/"+path])
            assert contents and b"grpc" in contents,path
            hashes[path]={"size":len(contents),"sha256":sha(contents)}
    write(name+"-outputs",hashes)
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
        for name in ["stock-scratch","ordinary","native"]:
            Path("MODULE.bazel").write_bytes(original)
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
