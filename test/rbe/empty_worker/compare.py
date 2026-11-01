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
    jq = r'''select(((.targetLabel // "")|test("^//debian:|^//tools/zstd:|^//tools/bazeldefs:version$|^//shim:config$|^//:release$|^//vdso:|^//runsc/(fdparking|prewarmer|procumounter):")) or .mnemonic=="GoLink" or .mnemonic=="PrecompileSeccomp" or .mnemonic=="ProtocAuthenticityCheck" or .mnemonic=="GoToolchainBinaryBuild" or ((.exitCode // 0)!=0) or ((.status // "")!="")) | {label:.targetLabel,mnemonic,runner,cacheHit,exitCode,status,action:.digest,platform:.platform,executable:.commandArgs[0],outputs:[.actualOutputs[]? | {path,digest}],argv:(if ((.targetLabel//"")|contains("systrap/sysmsg")) and (.mnemonic=="CppLink" or .mnemonic=="CppCompile" or .mnemonic=="Genrule") then .commandArgs else [] end)}'''
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
        status=subprocess.run(["timeout","--signal=INT","--kill-after=25s","45m",*cmd],stdout=stream,stderr=subprocess.STDOUT).returncode
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
cpu=os.environ["EXEC_CPU"]
machine,machine_name,repo={"arm64":(183,"aarch64","@bootstrap_busybox_arm64"),"amd64":(62,"x86_64","@bootstrap_busybox")}[cpu]
guest=repo+"//:payload/usr/bin/busybox"
files={"runsc", "containerd-shim-runsc-v1", "gvisor-bin/gvisor_sentry", "gvisor-bin/checkpointgofer",
       "gvisor-bin/runsc-metric-server", "gvisor-bin/runsc-fd-parking","gvisor-bin/gvisor-sentry-prewarmer","gvisor-bin/gvisor-proc-umounter"}
def elf(path):
    with path.open("rb") as stream:
        head=stream.read(24)
        assert head[:6]==b"\x7fELF\x02\x01" and int.from_bytes(head[18:20],"little")==machine
        stream.seek(0)
        return {"sha256":hashlib.file_digest(stream,"sha256").hexdigest(),"bytes":path.stat().st_size,"elf_machine":machine}
def inspect_tar(mode, fileobj, debian=False):
    binaries = {}
    modes = {}
    names = []
    with tarfile.open(fileobj=fileobj, mode=mode) as archive:
        for member in archive:
            name = member.name.removeprefix("./").lstrip("/")
            if not member.isfile():
                continue
            names.append(name)
            payload = name.removeprefix("usr/bin/") if debian else name
            if payload not in files:
                continue
            assert payload not in binaries, payload
            stream = archive.extractfile(member)
            header = stream.read(24)
            assert header[:6] == b"\x7fELF\x02\x01" and int.from_bytes(header[18:20],"little") == machine, payload
            digest = hashlib.sha256(header)
            for block in iter(lambda: stream.read(1024*1024), b""):
                digest.update(block)
            binaries[payload] = {"sha256":digest.hexdigest(),"bytes":member.size,"elf_machine":machine}
            modes[payload] = member.mode
            assert member.mode & 0o111, payload
    assert set(binaries) == files, (names, sorted(files-set(binaries)))
    if debian:
        assert "etc/containerd/runsc.toml" in names, names
    return binaries, modes, names

def inspect_package(work, path, kind):
    details = {"path":str(path.relative_to(work)),"bytes":path.stat().st_size}
    with path.open("rb") as archive:
        details["sha256"] = hashlib.file_digest(archive,"sha256").hexdigest()
        archive.seek(0)
        if kind=="bz2":
            contents = inspect_tar("r|bz2",archive)
        else:
            if kind=="deb":
                details["package"] = run("dpkg-deb","-f",path,"Package")
                details["architecture"] = run("dpkg-deb","-f",path,"Architecture")
                details["version"] = run("dpkg-deb","-f",path,"Version")
                assert (details["package"],details["architecture"],details["version"]) == ("runsc",cpu,version),details
                argv=["dpkg-deb","--fsys-tarfile",path]
            else:
                argv=["zstd","-q","-d","-c",path]
            with subprocess.Popen(argv,stdout=subprocess.PIPE) as proc:
                contents = inspect_tar("r|",proc.stdout,debian=kind=="deb")
                while proc.stdout.read(1024*1024):
                    pass
                assert proc.wait()==0
    details["binaries"],details["modes"],details["regular_member_names"] = contents
    return details

from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, build_opener
import zipfile

version = "20991231.0"
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        return None

def get_output(work, invocation, output):
    path=work/Path(output["path"]).name
    digest=output["digest"]
    size=int(digest.get("sizeBytes",0))
    uri="bytestream://remote.buildbuddy.io/blobs/"+digest["hash"]+"/"+str(size)
    url="https://app.buildbuddy.io/file/download?"+urlencode({"bytestream_url":uri,"invocation_id":invocation,"filename":path.name})
    req=Request(url,headers={"x-buildbuddy-api-key":os.environ["BUILDBUDDY_API_KEY"]})
    with build_opener(NoRedirect).open(req,timeout=120) as src,path.open("wb") as dest:
        shutil.copyfileobj(src,dest)
    with path.open("rb") as stream:
        assert path.stat().st_size==size and hashlib.file_digest(stream,"sha256").hexdigest()==digest["hash"]
    return path

def compressor(path):
    desc=elf(path)
    headers=run("readelf","-W","-l",path)
    dynamic=run("readelf","-W","-d",path)
    desc["interpreters"]=re.findall(r"\[Requesting program interpreter: (.*?)\]",headers)
    desc["needed"]=re.findall(r"\(NEEDED\).*?\[(.*?)\]",dynamic)
    return desc

def save_report(name,info):
    (out/(name+".json")).write_text(json.dumps(info,indent=2)+"\n")

def writer(actions, label, mnemonic, ext):
    items=[(a,o) for a in actions if a["label"]==label and a["mnemonic"]==mnemonic for o in a["outputs"] if o["path"].endswith(ext)]
    assert len(items)==1,(label,items)
    a,o=items[0]
    assert not a.get("exitCode") and not a.get("status"),a
    props={v["name"]:v["value"] for v in a["platform"]["properties"]}
    assert props.get("Arch")==cpu and props.get("container-image")==image,props
    return a,o

def get_old():
    path=tmp/"original.zip"
    expected={"amd64":"f5d6c356ff1a7037072d1f94402deaaa337811e6c808460bd9da4fa8c95983f8","arm64":"34ed5c2d69af36c83b463b37f4369279a543efced5aaff2ac8221b73419851f7"}[cpu]
    assert sha(path.read_bytes())==expected
    with zipfile.ZipFile(path) as z:
        build=json.loads(z.read("result.json"))[cpu]
        actions=json.loads(z.read(cpu+"/actions.json"))
    original=build["source"]
    assert original=="2dd42c8895f0de061285704a46b8de3cdaa6a1b4" and build["native_exit"]==1
    assert [a["label"] for a in build["failed_actions"]]==["//debian:gvisor-release-tar-zstd"]
    inv,=build["invocations"]
    directory=tmp/"original-product";directory.mkdir(exist_ok=True)
    rec={"source":original,"invocation":inv,"original_qualification_sha256":expected,"debian":build["packages"]["deb"],"read_started":now()}
    try:
        _,o=writer(actions,"//tools/zstd:compressor","CppLink","/compressor")
        rec["compressor"]=compressor(get_output(directory,inv,o))
        save_report("originals",rec)
        assert rec["compressor"]["interpreters"] and rec["compressor"]["needed"],rec["compressor"]
        a,o=writer(actions,"//debian:gvisor-release-tar-bz2","PackageTar",".tar.bz2")
        rec["bzip2"]=inspect_package(directory,get_output(directory,inv,o),"bz2")
        assert rec["bzip2"]["binaries"]==rec["debian"]["binaries"]
        rec["succeeded"]=True
    finally:
        rec["read_finished"]=now()
        save_report("originals",rec)

def get_new_compressor():
    r=json.loads((out/"result.json").read_text())[cpu]
    actions=json.loads((out/cpu/"actions.json").read_text())
    _,o=writer(actions,"//tools/zstd:compressor","CppLink","/compressor")
    d=tmp/"new-compressor";d.mkdir(exist_ok=True)
    inv,=r["invocations"]
    c=compressor(get_output(d,inv,o))
    save_report("corrected-compressor",{"source":r["source"],"invocation":inv,**c})
    assert c["interpreters"]==[] and c["needed"]==[],c

def packages(kind, names):
    expected={"deb":("//debian:debian","MakeDeb",".deb"),"bz2":("//debian:gvisor-release-tar-bz2","PackageTar",".tar.bz2"),"zstd":("//debian:gvisor-release-tar-zstd","PackageTar",".tar.zstd")}
    py_tool="//test/rbe/protoc_check:static_python"+("_arm64" if cpu=="arm64" else "")
    opts=["--compilation_mode=opt","--lockfile_mode=off","--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain","--extra_toolchains=//test/rbe/go_bootstrap:linux_"+cpu+"_ash,"+py_tool,"--action_env=PATH=/usr/bin:/bin","--modify_execution_info=^(PackageTar|MakeDeb)$=+no-remote-cache"]
    r,actions=invoke(kind,cpu,[expected[k][0] for k in names],opts)
    if r["native_exit"]:
        results["verdict"]="PACKAGE_BUILD_FAILED";save();raise SystemExit(r["native_exit"])
    work=Path(run("bazel","--bazelrc="+str(tmp/"sidecar.bazelrc"),"info","execution_root"))
    r["packages"]={}
    for k in names:
        a,o=writer(actions,*expected[k])
        info=inspect_package(work,work/o["path"],k)
        assert info["sha256"]==o["digest"]["hash"] and info["bytes"]==int(o["digest"]["sizeBytes"])
        assert a.get("runner")=="remote" and not a.get("cacheHit") and "-opt/" in o["path"],a
        info["producer"]={key:a.get(key) for key in ("action","runner","cacheHit","mnemonic","platform","executable")}
        r["packages"][k]=info;save()
    return r["packages"]

mode = sys.argv[1]
if mode=="originals":
    get_old();raise SystemExit
if mode=="inspect-compressor":
    get_new_compressor();raise SystemExit
try:
    assert platform.machine()==machine_name
    assert run("git","rev-parse","HEAD^")==os.environ["CORRECTED"]
    source(os.environ["CORRECTED"])
    assert run("git","diff","--name-only","HEAD^","HEAD")=="test/rbe/empty_worker/BUILD"
    subprocess.run(["git","tag","release-"+version,"HEAD"],check=True)
    assert run("tools/workspace_status.sh")=="STABLE_VERSION release-"+version
    results.update(cpu=cpu,mode="opt",fixture_local_tag="release-"+version,control=41258616758,repair="Only Linux scratch execution platforms declare @llvm//constraints/libc:musl; target platforms unchanged")
    new=packages(cpu,["zstd"])["zstd"]
    old=json.loads((out/"originals.json").read_text())
    assert old["succeeded"] and old["debian"]["binaries"]==old["bzip2"]["binaries"]
    keys=sorted(k for k,v in new["binaries"].items() if old["debian"]["binaries"].get(k)!=v)
    results["payload_changes_vs_previous_execution_platform"]=keys
    save()
    if keys:
        # Only if changing exec configuration changed the release ELFs, compare
        # the remaining two formats against precisely these same eight bytes.
        rest=packages("remaining-formats",["deb","bz2"])
        assert all(v["binaries"]==new["binaries"] for v in rest.values())
    results["verdict"]="THREE_OPT_DISTRIBUTIONS_SAME_EIGHT_ELFS"
    results["finished"]=now()
    save()
except BaseException as exc:
    results["exception"]=repr(exc)
    save()
    raise
