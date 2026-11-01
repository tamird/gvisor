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

try:
    assert platform.machine() == machine_name
    assert run("git","rev-parse","HEAD^") == os.environ["CORRECTED"]
    source(os.environ["CORRECTED"])
    version = "20991231.0"
    tag = "release-"+version
    subprocess.run(["git","tag",tag,"HEAD"],check=True)
    assert run("tools/workspace_status.sh") == "STABLE_VERSION "+tag
    targets = {"deb":"//debian:debian","bz2":"//debian:gvisor-release-tar-bz2","zstd":"//debian:gvisor-release-tar-zstd"}
    results.update(cpu=cpu,mode="opt",scoped_producer=".buildkite/release.yaml / Makefile artifacts/%",
                   fixture_local_tag=tag,runtime_source="f906c5dd8",earlier_fastbuild=41257521162,
                   worker_controls={"arm64":13061034514,"amd64":13056457271},archive_writer_backport="407a3c7c4c",
                   version_writer="316da5996",changelog_writer="8311c4cdb")
    py_tool="//test/rbe/protoc_check:static_python"+("_arm64" if cpu=="arm64" else "")
    opts=["--compilation_mode=opt","--lockfile_mode=off","--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
          "--extra_toolchains=//test/rbe/go_bootstrap:linux_"+cpu+"_ash,"+py_tool,
          "--action_env=PATH=/usr/bin:/bin","--modify_execution_info=^(PackageTar|MakeDeb)$=+no-remote-cache"]
    r, actions = invoke(cpu,cpu,list(targets.values()),opts)
    failed = [a for a in actions if a.get("exitCode") or a.get("status")]
    r["failed_actions"]=[{k:a.get(k) for k in ("label","mnemonic","runner","exitCode","status","executable","outputs","platform")} for a in failed]
    save()
    work=Path(run("bazel","--bazelrc="+str(tmp/"sidecar.bazelrc"),"info","execution_root"))
    r["packages"]={}
    missing=[]
    problems=[]
    endings={"deb":".deb","bz2":".tar.bz2","zstd":".tar.zstd"}
    for name,label in targets.items():
        possible=[(a,o) for a in actions if a.get("label")==label for o in a.get("outputs",[]) if o.get("path","").endswith(endings[name])]
        if len(possible)!=1:
            missing.append({"label":label,"matched_outputs":[o for a,o in possible]})
            continue
        a,o=possible[0]
        try:
            p=work/o["path"]
            pkg=inspect_package(work,p,name)
            pkg.update(producer={k:a.get(k) for k in ("action","runner","cacheHit","mnemonic","platform","executable")})
            assert pkg["sha256"]==o["digest"]["hash"] and pkg["bytes"]==int(o["digest"]["sizeBytes"]),label
            assert "-opt/" in o["path"],o
            props={v["name"]:v["value"] for v in a["platform"]["properties"]}
            assert (props.get("Arch"),props.get("container-image"))==(cpu,image),props
            assert a["runner"]=="remote" and not a.get("cacheHit"), a
            r["packages"][name]=pkg
        except Exception as exc:
            problems.append({"label":label,"failure":repr(exc)})
        save()
    r["missing_outputs"]=missing
    r["verification_errors"]=problems
    if r["native_exit"] or failed or missing or problems:
        results["verdict"]="INCOMPLETE";save();raise SystemExit(1)
    assert all(p["binaries"]==r["packages"]["deb"]["binaries"] for p in r["packages"].values())
    results["verdict"]="THREE_OPT_DISTRIBUTIONS_SAME_EIGHT_ELFS"
    results["complete"]=now()
    save()
except BaseException as exc:
    results["exception"]=repr(exc)
    save()
    raise
