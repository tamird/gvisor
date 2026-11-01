from datetime import datetime, timezone
from pathlib import Path
import platform
import shutil
from urllib.request import Request, urlopen
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile

tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp/"package-evidence"
out.mkdir(exist_ok=True)
image = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
gnu = "docker://mirror.gcr.io/library/gcc@sha256:66035d353338cb93b64f621393dc6fecde85258651ca454f0cf36ff2639b1352"
pkg = "//pkg/sentry/platform/systrap/sysmsg:"
package = "//pkg/gohacks:gohacks"
probe = "//test/rbe/empty_worker:root"
results = {"base":os.environ["BASE"],"original":os.environ["ORIGINAL"],"corrected":os.environ["CORRECTED"],"carrier":os.environ["GITHUB_SHA"],"worker_image":image,"client":"Ubuntu 24.04; repository evaluation, receipt inspection and workspace-status script on the client"}
def now():
    return datetime.now(timezone.utc).isoformat()
def run(*args):
    return subprocess.check_output(args, text=True).strip()
def save():
    (out/"result.json").write_text(json.dumps(results, indent=2)+"\n")
def sha(data):
    return hashlib.sha256(data).hexdigest()
def provenance():
    def get(url, headers=None):
        with urlopen(Request(url, headers=headers or {}),timeout=45) as response:
            return response.read()
    def check(endpoint, key):
        data = get(endpoint+key, headers)
        assert "sha256:"+sha(data)==key
        return data
    token = json.loads(get("https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/hello-world:pull"))["token"]
    headers = {"Authorization":"Bearer "+token,"Accept":"application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json"}
    root = "https://registry-1.docker.io/v2/library/hello-world/"
    index = image.split("@")[1]
    entries = json.loads(check(root+"manifests/",index))
    manifest, = [e["digest"] for e in entries["manifests"] if e["platform"].get("architecture")=="arm64" and e["platform"].get("os")=="linux"]
    layer, = json.loads(check(root+"manifests/",manifest))["layers"]
    data = check(root+"blobs/",layer["digest"])
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        name, = archive.getnames()
        assert name=="hello" and archive.getmember(name).isreg()
        hello=sha(archive.extractfile(name).read())
    return dict(index=index,manifest=manifest,layer=layer["digest"],layer_bytes=len(data),image_paths=[name],hello_sha256=hello)
def source(ref):
    changed=run("git","diff","--name-only").splitlines()
    if changed:
        assert changed==["MODULE.bazel.lock"], changed
        results.setdefault("client_updated_module_lock",[]).append({"source":run("git","rev-parse","HEAD"),"sha256":sha(Path("MODULE.bazel.lock").read_bytes())})
        subprocess.run(["git","restore","--","MODULE.bazel.lock"],check=True)
    subprocess.run(["git","checkout","--detach",ref],check=True)
    assert run("git","rev-parse","HEAD")==ref
    subprocess.run(["git","diff","--quiet"],check=True)
def rows(log):
    if not log.exists(): return []
    # Keep the argv, result, platform and output digests for relevant actions only.
    jq = r'''select(((.targetLabel // "")|test("^//vdso:|^//runsc:|^//runsc/boot/filter:filter$")) or .mnemonic=="PrecompileSeccomp" or .mnemonic=="ProtocAuthenticityCheck" or .mnemonic=="GoToolchainBinaryBuild" or ((.exitCode // 0)!=0) or ((.status // "")!="")) | {label:.targetLabel,mnemonic,runner,cacheHit,exitCode,status,action:.digest,platform:.platform,executable:.commandArgs[0],outputs:[.actualOutputs[]? | {path,digest}],argv:(if ((.targetLabel//"")|contains("systrap/sysmsg")) and (.mnemonic=="CppLink" or .mnemonic=="CppCompile" or .mnemonic=="Genrule") then .commandArgs else [] end)}'''
    with subprocess.Popen([str(tmp/"bb"),"print","--compact_execution_log="+str(log)],stdout=subprocess.PIPE) as decode:
        raw = subprocess.check_output(["jq","-c",jq],stdin=decode.stdout)
        decode.stdout.close()
        assert decode.wait()==0
    return [json.loads(line) for line in raw.splitlines()]
def invoke(kind,arch,targets,options=()):
    folder=out/kind; folder.mkdir(exist_ok=True)
    log=tmp/(kind+".binpb");txt=tmp/(kind+".buildlog");bep=tmp/(kind+".bep")
    plat="//test/rbe/empty_worker:"+arch
    cmd=["bazel","--bazelrc="+str(tmp/"sidecar.bazelrc"),"build","--config="+("aarch64" if arch=="arm64" else "x86_64"),"--keep_going","--jobs=40","--remote_accept_cached","--noremote_upload_local_results","--remote_download_outputs=toplevel","--spawn_strategy=remote","--noremote_local_fallback","--host_platform="+plat,"--extra_execution_platforms="+plat,"--execution_log_compact_file="+str(log),"--build_event_json_file="+str(bep),"--nobuild_event_json_file_path_conversion","--color=no","--curses=no",*options,*targets]
    start=now()
    with txt.open("w") as stream:
        status=subprocess.run(["timeout","--signal=INT","--kill-after=25s","31m",*cmd],stdout=stream,stderr=subprocess.STDOUT).returncode
    finished=now()
    original=txt.read_text(errors="replace")
    messages=[l.strip()[:1300] for l in original.splitlines() if re.search(r"ERROR:|Traceback|Error in|in fail|No such file|could not start|failed to start|not found|undefined symbol|unable to find|permission denied|unrecognized option|Invocation ID:",l,re.I) and not re.search(r"api.key|authorization|credential",l,re.I)]
    r={"source":run("git","rev-parse","HEAD"),"targets":targets,"argv":cmd[2:],"native_exit":status,"started":start,"finished":finished,"invocations":re.findall(r"Invocation ID: ([0-9a-f-]{36})",original),"errors":messages[-32:]}
    records=[]
    lines=original.splitlines()
    for index,line in enumerate(lines):
        if line.startswith("ERROR:") or line.startswith("panic:") or "permission denied" in line.lower() or "not found" in line.lower():
            block="\n".join(lines[index:index+14])
            if re.search(r"api[-_]key|authorization|credential|remote_header",block,re.I): continue
            if len(records)<60: records.append(block[:3600])
    (folder/"diagnostics.txt").write_text("\n---\n".join(records)+"\n")
    results[kind]=r; save()
    actions=rows(log)
    (folder/"actions.json").write_text(json.dumps(actions,indent=2)+"\n")
    r["actions"]=[{k:a.get(k) for k in ("label","mnemonic","runner","cacheHit","exitCode","status","executable")} for a in actions]
    print(kind,status,r["invocations"],messages[-5:],flush=True);save()
    return r,actions
try:
    assert platform.machine()=="aarch64"
    assert run("git","rev-parse","HEAD^")==os.environ["CORRECTED"]
    source(os.environ["CORRECTED"])
    results["original_controls"]={"amd64":13056457271,"arm64":13061034514,
                                  "arm64_filter":13062876059}
    target="//runsc:runsc"
    for cpu,machine in (("arm64",183),("amd64",62)):
        try:
            opts=["--compilation_mode=fastbuild","--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
                  "--extra_toolchains=//test/rbe/go_bootstrap:linux_"+cpu+"_ash"]
            result,actions=invoke(cpu,cpu,[target],opts)
            failed=[a for a in actions if a.get("exitCode") or a.get("status")]
            result["failed_actions"]=[{k:a.get(k) for k in ("label","mnemonic","executable","runner","exitCode","status","action")} for a in failed]
            def props(a):
                return {p["name"]:p["value"] for p in a["platform"]["properties"]}
            if result["native_exit"]==0:
                rows=[(a,o) for a in actions if a["mnemonic"]=="GoLink" and a["label"].startswith("//runsc:") for o in a["outputs"] if Path(o["path"]).name=="runsc"]
                tool,product=next((a,o) for a,o in rows if o["path"].split("/")[1].startswith("aarch64-" if cpu=="arm64" else "k8-"))
                assert props(tool)["Arch"]==cpu and props(tool)["container-image"]==image
                result["owner_action"]={k:tool.get(k) for k in ("label","runner","cacheHit","platform","action","executable")}
                result["owner_output"]=product
                save()
                exec_root=Path(run("bazel","--bazelrc="+str(tmp/"sidecar.bazelrc"),"info","execution_root"))
                assert exec_root.is_absolute()
                file=exec_root/product["path"]
                with file.open("rb") as stream:
                    head=stream.read(24)
                    assert head[:6]==b"\x7fELF\x02\x01" and int.from_bytes(head[18:20],"little")==machine
                    stream.seek(0)
                    digest=hashlib.file_digest(stream,"sha256").hexdigest()
                assert file.stat().st_size==int(product["digest"]["sizeBytes"]) and digest==product["digest"]["hash"]
                result.update(verdict="RUNSC_BUILT",bytes=file.stat().st_size,sha256=digest,elf_machine=machine)
            elif any(x.get("runner")=="remote" and x.get("platform") and props(x).get("Arch")==cpu and props(x).get("container-image")==image for x in failed):
                result["verdict"]="MEASURED_WORKER_FAILURE"
            else:
                result["verdict"]="NO_WORKER_WITNESS"
        except Exception as err:
            results.setdefault(cpu,{})["verification_error"]=repr(err)[:600]
            results[cpu]["verdict"]="INCOMPLETE"
        finally:
            save()
    results["verdict"]="ALL_ARMS_OBSERVED" if all(results[cpu]["verdict"] in ("RUNSC_BUILT","MEASURED_WORKER_FAILURE") for cpu in ("arm64","amd64")) else "INCOMPLETE"
except Exception as err:
    results["error"]=repr(err);results["verdict"]="INCOMPLETE"
finally:
    results["verified"]=now();save()
    print(results["verdict"],[(cpu,results.get(cpu,{}).get("verdict")) for cpu in ("arm64","amd64")],flush=True)
sys.exit(results["verdict"]!="ALL_ARMS_OBSERVED")
