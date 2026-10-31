from datetime import datetime, timezone
from pathlib import Path
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
gnu = "docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a"
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
    manifest, = [e["digest"] for e in entries["manifests"] if e["platform"].get("architecture")=="amd64" and e["platform"].get("os")=="linux"]
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
    jq = r'''select(((.targetLabel // "")|test("pkg/sentry/platform/systrap/sysmsg|pkg/gohacks|test/rbe/empty_worker")) or .mnemonic=="GoToolchainBinaryBuild" or ((.exitCode // 0)!=0) or ((.status // "")!="")) | {label:.targetLabel,mnemonic,runner,cacheHit,exitCode,status,action:.digest,platform:.platform,executable:.commandArgs[0],outputs:[.actualOutputs[]? | {path,digest}],argv:(if ((.targetLabel//"")|contains("systrap/sysmsg")) and (.mnemonic=="CppLink" or .mnemonic=="CppCompile" or .mnemonic=="Genrule") then .commandArgs else [] end)}'''
    with subprocess.Popen([str(tmp/"bb"),"print","--compact_execution_log="+str(log)],stdout=subprocess.PIPE) as decode:
        raw = subprocess.check_output(["jq","-c",jq],stdin=decode.stdout)
        decode.stdout.close()
        assert decode.wait()==0
    return [json.loads(line) for line in raw.splitlines()]
def invoke(kind,arch,targets,options=()):
    folder=out/kind; folder.mkdir(exist_ok=True)
    log=tmp/(kind+".binpb");txt=tmp/(kind+".buildlog");bep=tmp/(kind+".bep")
    plat="//test/rbe/empty_worker:"+arch
    cmd=["bazel","--bazelrc="+str(tmp/"sidecar.bazelrc"),"build","--config=x86_64","--keep_going","--jobs=40","--noremote_accept_cached","--noremote_upload_local_results","--remote_download_outputs=toplevel","--spawn_strategy=remote","--noremote_local_fallback","--host_platform="+plat,"--extra_execution_platforms="+plat,"--execution_log_compact_file="+str(log),"--build_event_json_file="+str(bep),"--nobuild_event_json_file_path_conversion","--color=no","--curses=no",*options,*targets]
    start=now()
    with txt.open("w") as stream:
        status=subprocess.run(cmd,stdout=stream,stderr=subprocess.STDOUT).returncode
    finished=now()
    original=txt.read_text(errors="replace")
    messages=[l.strip()[:1300] for l in original.splitlines() if re.search(r"ERROR:|No such file|could not start|failed to start|not found|undefined symbol|unable to find|permission denied|unrecognized option|Invocation ID:",l,re.I) and not re.search(r"api.key|authorization|credential",l,re.I)]
    r={"source":run("git","rev-parse","HEAD"),"targets":targets,"argv":cmd[2:],"native_exit":status,"started":start,"finished":finished,"invocations":re.findall(r"Invocation ID: ([0-9a-f-]{36})",original),"errors":messages[-32:]}
    results[kind]=r; save()
    actions=rows(log)
    (folder/"actions.json").write_text(json.dumps(actions,indent=2)+"\n")
    r["actions"]=[{k:a.get(k) for k in ("label","mnemonic","runner","cacheHit","exitCode","status","executable")} for a in actions]
    print(kind,status,r["invocations"],messages[-5:],flush=True);save()
    return r,actions
def remote(a,img):
    props={p["name"]:p["value"] for p in (a.get("platform") or {}).get("properties",[])}
    return a.get("runner")=="remote" and not a.get("cacheHit") and props.get("container-image")==img and props.get("Arch")=="amd64"
def collect_root(kind,img):
    result,actions=invoke(kind,"amd64" if img==image else "gnu",[probe])
    if result["native_exit"]:return False
    data=Path("bazel-bin/test/rbe/empty_worker/root.txt").read_bytes()
    (out/kind/"filesystem.txt").write_bytes(data)
    obs={name:(value,int(rc)) for name,value,rc in re.findall(r"^exec (\S+) (yes|no) (\d+)$",data.decode(),re.M)}
    result["probe"]=obs
    result["root_paths"]=re.findall(r"^root (\S+)$",data.decode(),re.M)
    act,=[a for a in actions if a["mnemonic"]=="ObserveWorkerRoot"]
    seen=set(obs)=={"/bin/sh","/bin/bash","/usr/bin/cc","/usr/bin/gcc","/usr/bin/clang"}
    verified=remote(act,img) and not act.get("exitCode") and any(o.get("digest",{}).get("hash")==sha(data) for o in act["outputs"])
    if img==image:
        content=re.search(r"^hello-sha (\w+)$",data.decode(),re.M)
        return verified and seen and "/hello" in result["root_paths"] and content and content[1]==results["provenance"]["hello_sha256"] and all(e==("no",127) for e in obs.values())
    return verified and seen and obs["/bin/sh"]==("yes",0) and any(obs[k]==("yes",0) for k in ("/usr/bin/gcc","/usr/bin/cc"))
def verify_product(r,actions):
    if r["native_exit"]: return False
    successful=[a for a in actions if remote(a,image) and not a.get("exitCode") and not a.get("status")]
    def matches(m,s):
        return [a for a in successful if a.get("mnemonic")==m and s in (a.get("label") or "")]
    ok = len(matches("CppCompile","systrap/sysmsg"))>=4 and bool(matches("CppLink","systrap/sysmsg")) and bool(matches("GoCompilePkg","pkg/gohacks")) and bool(matches("GoLink","systrap/sysmsg")) and bool(matches("GoToolchainBinaryBuild",""))
    sd=Path("bazel-bin/pkg/sentry/platform/systrap/sysmsg")
    elf=sd/"sighandler.built-in.bin.o";blob=sd/"sighandler.built-in.bin";offsets=sd/"sighandler.go.arch"
    section=tmp/"section.bin"
    subprocess.run(["objcopy","--dump-section",".crblob="+str(section),str(elf),str(tmp/"readback.elf")],check=True)
    exports=dict((name,int(value,16)) for value,name in re.findall(r"^([0-9a-fA-F]+)\s+\w\s+(__export_\w+)$",run("nm","-n",str(elf)),re.M))
    written=dict((name,int(value,16)) for name,value in re.findall(r"Sighandler_blob_offset__(__export_\w+) = (0x\w+)",offsets.read_text()))
    assert exports and exports==written
    ok = ok and blob.read_bytes()==section.read_bytes() and 0<len(blob.read_bytes())<=4096 and int.from_bytes(elf.read_bytes()[18:20],"little")==62
    r["materialized"]={p.name:{"bytes":p.stat().st_size,"sha256":sha(p.read_bytes())} for p in (elf,blob,offsets)}
    r["exported_offsets"]=exports
    ok = ok and all(any(o.get("digest",{}).get("hash")==m["sha256"] for a in successful for o in a["outputs"]) for m in r["materialized"].values())
    r["go_archive_outputs"]=[o for a in matches("GoCompilePkg","pkg/gohacks") for o in a["outputs"]]
    return ok and bool(r["go_archive_outputs"])
try:
    assert run("git","rev-parse","HEAD^")==os.environ["CORRECTED"]
    assert run("git","rev-parse","HEAD~2")==os.environ["ORIGINAL_CARRIER"]
    results["retained_controls"]={"carrier":os.environ["ORIGINAL_CARRIER"],"run":41237444729,"artifact":13055412963,"sha256":"02d0ae4ceb37e86e9ba474d8590e930605d20df2b72a854534a533eae9496850"}
    labels=[pkg+"sighandler_object",pkg+"sighandler.built-in.bin",package]
    source(os.environ["CORRECTED"])
    opts=["--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain","--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash"]
    product,actions=invoke("composed","amd64",[*labels,pkg+"sighandler.go.arch",pkg+"gen_stub"],opts)
    results["real_product_ok"]=bool(verify_product(product,actions))
except Exception as e:
    results["error"]=repr(e)
finally:
    passed=bool(results.get("retained_controls") and results.get("real_product_ok"))
    results["verdict"]="VERIFIED" if passed else "INCOMPLETE"
    results["verified"]=now();save()
    print(results["verdict"],results.get("error",""),flush=True)
sys.exit(not passed)
