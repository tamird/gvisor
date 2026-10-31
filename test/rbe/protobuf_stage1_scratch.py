"""Build the supplier's six stage-1 targets and verify their executed outputs."""
import hashlib
import json
import os
import pathlib
import subprocess
import sys

tmp = pathlib.Path(os.environ["RUNNER_TEMP"])
workspace = pathlib.Path(os.environ["GITHUB_WORKSPACE"])
source = workspace / "supplier"
consumer = tmp / "protobuf-consumer"
records = tmp / "stage1-evidence"
records.mkdir(exist_ok=True)
parent = "d2a1de9832fbb3109ac5bedd37bd9faef270e382"
candidate = "a4504c5467dde4ef8ee4a075ee240c54a7da4883"
image = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
owners = {}
for package, stem, logical in [
    ("upb/reflection", "descriptor", "google/protobuf/descriptor"),
    ("upb/reflection", "json_enumvalue_options", "google/protobuf/json_enumvalue_options"),
    ("upb_generator", "plugin", "google/protobuf/compiler/plugin"),
]:
    for plugin in ("upb", "upb_minitable"):
        key = f"{package}:gen_{stem}_upb_proto_{plugin}_stage1"
        files = [f"{package}/stage1/{logical}.{plugin}.{ext}" for ext in (["h"] if plugin == "upb" else ["h","c"])]
        owners[key] = files
targets = ["@protobuf//" + k for k in owners]

def read_git(*args):
    return subprocess.check_output(["git", "-C", source, *args], text=True).strip()

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

def digest(data):
    return hashlib.sha256(data).hexdigest()

def lane(name, rev):
    fixed = name == "scratch"
    dst = records / name
    dst.mkdir()
    subprocess.run(["git","-C",source,"checkout","--quiet","--detach",rev],check=True)
    assert read_git("rev-parse","HEAD")==rev
    rcfile = tmp / "stage1.bazelrc"
    logfile = tmp / (name + "-bazel.log")
    execlog = tmp / (name + "-spawns.json")
    key = os.environ["BUILDBUDDY_API_KEY"]
    with rcfile.open("w") as out:
        os.chmod(rcfile, 0o600)
        print("build --bes_backend=grpcs://remote.buildbuddy.io", file=out)
        print("build --bes_results_url=https://app.buildbuddy.io/invocation/", file=out)
        print("build --bes_header=x-buildbuddy-api-key=" + key, file=out)
        if fixed:
            print("build --remote_header=x-buildbuddy-api-key=" + key, file=out)
            print("build --remote_executor=grpcs://remote.buildbuddy.io", file=out)
            for k,v in [("Arch","amd64"),("OSFamily","linux"),("container-image",image),("workload-isolation-type","oci")]:
                print(f"build --remote_default_exec_properties={k}={v}", file=out)
    cmd = ["bazelisk", "--bazelrc="+str(rcfile), "build", "--jobs=3", "--spawn_strategy=local",
           "--noremote_local_fallback","--noremote_upload_local_results","--noremote_accept_cached",
           "--host_cxxopt=-std=c++17","--cxxopt=-std=c++17","--features=fully_static_link","--host_features=fully_static_link",
           "--host_linkopt=-static","--nostamp", "--@protobuf//bazel/toolchains:prefer_prebuilt_protoc=false",
           "--execution_log_json_file="+str(execlog)]
    if fixed:
        cmd += ["--strategy=GenUpbStage1=remote","--remote_download_outputs=toplevel"]
    with logfile.open("w") as out:
        proc=subprocess.run(cmd+targets,cwd=consumer, stdout=out, stderr=subprocess.STDOUT)
    raw=logfile.read_text(errors="replace").replace(key,"[REDACTED]")
    (dst/"invocation.txt").write_text("\n".join(l for l in raw.splitlines() if "Invocation ID:" in l)+"\n")
    errors = [l for l in raw.splitlines() if "ERROR:" in l or "error:" in l or "fatal:" in l]
    (dst/"diagnostics.txt").write_text("\n".join((errors[:30]+raw.splitlines()[-49:]))+"\n")
    result={"source":rev,"bazel_exit":proc.returncode,"kind":"native local Linux" if not fixed else "remote shell-free AMD64","actions":{},"outputs":{}}
    # Capture the original six spawns and files before interpreting the exercise.
    capture = []
    if execlog.is_file():
        for spawn in spawns(execlog):
            if spawn.get("targetLabel","").partition("//")[2] in owners:
                capture.append({k: spawn.get(k) for k in ("targetLabel","mnemonic","commandArgs","platform","listedOutputs","actualOutputs","runner","cacheHit","remoteCacheHit","exitCode","status")})
    (dst/"original-spawns.json").write_text(json.dumps(capture,indent=2)+"\n")
    for p in (consumer/"bazel-bin").glob("external/protobuf*/**/stage1/google/protobuf/**/*"):
        if p.is_file() and p.suffix in (".h",".c"):
            dest=dst/"original-files"/p.relative_to(consumer/"bazel-bin")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(p.read_bytes())
    try:
        if proc.returncode:
            raise ValueError(f"{name}: Bazel exited {proc.returncode}: " + " ".join(errors[:2])[:600])
        for spawn in spawns(execlog):
            label = spawn.get("targetLabel","").partition("//")[2]
            if label not in owners:
                continue
            if spawn.get("mnemonic") != ("GenUpbStage1" if fixed else "Genrule"):
                continue
            assert label not in result["actions"], label
            args=spawn["commandArgs"]
            props={p["name"]:p["value"] for p in spawn.get("platform",{}).get("properties",[])}
            outs=spawn.get("listedOutputs", [])
            assert sorted(next(f for f in owners[label] if p.endswith("/"+f)) for p in outs)==sorted(owners[label]), (label,outs)
            action={"runner":spawn.get("runner"),"cached":spawn.get("cacheHit",False) or spawn.get("remoteCacheHit",False),"platform":props,"args":args,"outputs":outs,"exit":spawn.get("exitCode",0)}
            result["actions"][label]=action
            assert not action["cached"] and action["exit"]==0
            if fixed:
                assert action["runner"]=="remote",action
                assert props.get("Arch")=="amd64" and props.get("container-image")==image,props
                assert args[0].endswith("/protoc_minimal"),args
                assert len([a for a in args if a.startswith("--plugin=protoc-gen-") and a.endswith("_stage0")])==1,args
                assert any(a.startswith("-I") and a.endswith("/../..") for a in args)
                assert any("_out=bootstrap_stage=1:" in a and a.endswith("/stage1") for a in args)
            else:
                assert action["runner"] in ("local","processwrapper-sandbox","linux-sandbox") and args[0].endswith("/bash"),action
        assert result["actions"].keys()==owners.keys(), (sorted(result["actions"]),sorted(owners))
        for relative_name in [p for files in owners.values() for p in files]:
            bases = [consumer / "bazel-bin" / "external" / "protobuf+"]
            matches = [p / relative_name for p in bases if (p / relative_name).is_file()]
            assert len(matches)==1,relative_name
            data=matches[0].read_bytes()
            assert data,relative_name
            result["outputs"][relative_name]=digest(data)
            dest=dst/relative_name
            dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_bytes(data)
        return result
    finally:
        (dst/"actions.json").write_text(json.dumps(result,indent=2)+"\n")

def main():
    assert read_git("rev-parse",candidate+"^")==parent
    assert read_git("diff","--name-only",parent,candidate)=="upb_generator/bootstrap_compiler.bzl"
    consumer.mkdir()
    (consumer/"MODULE.bazel").write_text(f'module(name="stage1_consumer")\nbazel_dep(name="protobuf",version="37.0")\nlocal_path_override(module_name="protobuf",path="{source}")\n')
    (consumer/"BUILD.bazel").touch()
    failures = []
    captured = {}
    for name,rev in [("ordinary",parent),("scratch",candidate)]:
        try:
            captured[name]=lane(name,rev)
        except Exception as error:
            failures.append([name,str(error)])
    if failures:
        raise ValueError(failures)
    stock,fixed=captured["ordinary"],captured["scratch"]
    assert len(stock["outputs"])==9 and stock["outputs"]==fixed["outputs"],(stock["outputs"],fixed["outputs"])
    return {"status":"PASS","baseline":parent,"product":candidate,"six_actions":list(owners),"matching_files":stock["outputs"]}

if __name__=="__main__":
    try:
        verdict=main()
    except Exception as error:
        verdict={"status":"FAIL","error":repr(error)}
    (records/"verdict.json").write_text(json.dumps(verdict,indent=2)+"\n")
    print(json.dumps(verdict))
    sys.exit(verdict["status"]!="PASS")
