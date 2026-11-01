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
    jq = r'''select(((.targetLabel // "")|test("^//:release$|^//vdso:")) or .mnemonic=="GoLink" or .mnemonic=="PrecompileSeccomp" or .mnemonic=="ProtocAuthenticityCheck" or .mnemonic=="GoToolchainBinaryBuild" or ((.exitCode // 0)!=0) or ((.status // "")!="")) | {label:.targetLabel,mnemonic,runner,cacheHit,exitCode,status,action:.digest,platform:.platform,executable:.commandArgs[0],outputs:[.actualOutputs[]? | {path,digest}],argv:(if ((.targetLabel//"")|contains("systrap/sysmsg")) and (.mnemonic=="CppLink" or .mnemonic=="CppCompile" or .mnemonic=="Genrule") then .commandArgs else [] end)}'''
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
def smoke(install,busybox):
    bundle=tmp/"guest-bundle"; fs=bundle/"rootfs"; fs.mkdir(parents=True)
    (fs/"proc").mkdir()
    shutil.copy2(busybox,fs/"busybox")
    (fs/"busybox").chmod(0o755)
    expected="release-"+cpu+"-"+results["corrected"][:9]
    (fs/"probe").write_text(expected)
    process={"terminal":False,"user":{"uid":0,"gid":0},"cwd":"/",
             "args":["/busybox","sh","-ec",'/busybox cat /probe; printf "\\n"; /busybox uname -m'],"env":["PATH=/"]}
    spec={"ociVersion":"1.0.2","root":{"path":"rootfs"},"process":process,
          "mounts":[{"type":"proc","source":"proc","destination":"/proc"}],
          "linux":{"namespaces":[{"type":n} for n in ("pid","mount","uts","ipc","network")]}}
    (bundle/"config.json").write_text(json.dumps(spec))
    state=tmp/"oci-state";state.mkdir()
    apparmor=Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
    if apparmor.exists() and apparmor.read_text().strip()!="0":
        subprocess.run(["sudo","sysctl","-w","kernel.apparmor_restrict_unprivileged_userns=0"],check=True)
    args=["sudo","-n","--",str(install/"runsc"),"--root="+str(state),"--platform=systrap","--network=none",
          "--overlay2=none","--host-settings=ignore","--ignore-cgroups"]
    cid="scratch-release"
    sub=results["native_smoke"]={"privileged_host":platform.platform(),"started":now(),"guest":guest,
        "elf":elf(fs/"busybox"),"read_from_guest":expected,"runsc_flags":args[4:],"sidecar_source":"adjacent gvisor-bin from //:release"}
    save()
    try:
        proc=subprocess.run(["timeout","--kill-after=5","90",*args,"run","--bundle="+str(bundle),cid],
                            stdin=subprocess.DEVNULL,capture_output=True,text=True)
        sub.update(exit=proc.returncode,stdout=proc.stdout[:1000],stderr=proc.stderr[-5000:],finished=now())
        assert proc.returncode==0 and proc.stdout.splitlines()==[expected,machine_name], "guest did not emit both expected outputs"
        sub["verdict"]="GUEST_EXECUTED"
    finally:
        subprocess.run(["timeout","20",*args,"delete","--force",cid],capture_output=True)
        save()
try:
    assert platform.machine()==machine_name
    assert run("git","rev-parse","HEAD^")==os.environ["CORRECTED"]
    source(os.environ["CORRECTED"])
    results.update(cpu=cpu,mode="fastbuild",public_target="//:release",declared_staging="679ce6be6e12e3d5d896b96294be4dd59473537f",
                   original_worker_controls={"arm64":13061034514,"amd64":13056457271},private_cli_evidence=13063272315)
    opts=["--compilation_mode=fastbuild","--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
          "--extra_toolchains=//test/rbe/go_bootstrap:linux_"+cpu+"_ash"]
    result,actions=invoke(cpu,cpu,["//:release",guest],opts)
    failed=[a for a in actions if a.get("exitCode") or a.get("status")]
    result["failed_actions"]=[{k:a.get(k) for k in ("label","mnemonic","executable","runner","exitCode","status","action","platform")} for a in failed]
    save()
    if result["native_exit"]!=0:
        results["verdict"]="BUILD_FAILED";raise SystemExit(1)
    stage=[(a,o) for a in actions if a["label"]=="//:release" for o in a["outputs"] if "/bin/release/" in o["path"]]
    if {o["path"].split("/bin/release/",1)[1] for a,o in stage}!=files:
        raise ValueError("public release staging outputs were missing from the compact log: "+str([o["path"] for a,o in stage]))
    execroot=Path(run("bazel","--bazelrc="+str(tmp/"sidecar.bazelrc"),"info","execution_root"))
    install=tmp/"native-release";install.mkdir()
    result["artifacts"]={}
    for a,o in stage:
        rel=o["path"].split("/bin/release/",1)[1]
        p=execroot/o["path"]
        info=elf(p)
        assert info["sha256"]==o["digest"]["hash"] and info["bytes"]==int(o["digest"]["sizeBytes"])
        props={item["name"]:item["value"] for item in a["platform"]["properties"]}
        assert props.get("Arch")==cpu and props.get("container-image")==image
        dest=install/rel;dest.parent.mkdir(exist_ok=True);shutil.copy2(p,dest)
        assert os.access(dest,os.X_OK)
        result["artifacts"][rel]=dict(**info,mnemonic=a["mnemonic"],runner=a.get("runner"),cache_hit=a.get("cacheHit",False),action=a["action"],worker=props)
    result["verdict"]="EIGHT_RELEASE_ELFS_VERIFIED";save()
    # Fetch Bazel's actual declared source file, not a client system utility.
    cquery=run("bazel","--bazelrc="+str(tmp/"sidecar.bazelrc"),"cquery","--output=files",guest)
    paths=[execroot/line for line in cquery.splitlines() if line.endswith("/payload/usr/bin/busybox")]
    if len(paths)!=1: raise ValueError("expected one BusyBox source path; got "+repr(cquery[:350]))
    smoke(install,paths[0])
    results["verdict"]="RELEASE_FILES_AND_NATIVE_GUEST_VERIFIED"
except Exception as exc:
    results["error"]=type(exc).__name__+": "+str(exc)[:400]
    results["verdict"]="INCOMPLETE"
finally:
    results["verified"]=now();save()
    print(cpu,results.get("verdict"),results.get("error"),flush=True)
sys.exit(results["verdict"]!="RELEASE_FILES_AND_NATIVE_GUEST_VERIFIED")
