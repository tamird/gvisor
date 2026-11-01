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
results = {"corrected":os.environ["CORRECTED"],"carrier":os.environ["GITHUB_SHA"],"worker_image":image,"client":"Ubuntu 24.04; repository evaluation, receipt inspection and workspace-status script on the client"}
def now():
    return datetime.now(timezone.utc).isoformat()
def run(*args):
    return subprocess.check_output(args, text=True).strip()
def save():
    (out/"result.json").write_text(json.dumps(results, indent=2)+"\n")
def sha(data):
    return hashlib.sha256(data).hexdigest()
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
    jq = r'''select(((.targetLabel // "")|test("^//:release$|^//vdso:|^//runsc/(fdparking|prewarmer|procumounter):")) or .mnemonic=="GoLink" or .mnemonic=="PrecompileSeccomp" or .mnemonic=="ProtocAuthenticityCheck" or .mnemonic=="GoToolchainBinaryBuild" or ((.exitCode // 0)!=0) or ((.status // "")!="")) | {label:.targetLabel,mnemonic,runner,cacheHit,exitCode,status,action:.digest,platform:.platform,executable:.commandArgs[0],raceFlags:(if .mnemonic=="GoLink" then [.commandArgs[]?|select(.=="-race" or .=="-static")] else [] end),outputs:[.actualOutputs[]? | {path,digest}],argv:(if ((.targetLabel//"")|contains("systrap/sysmsg")) and (.mnemonic=="CppLink" or .mnemonic=="CppCompile" or .mnemonic=="Genrule") then .commandArgs else [] end)}'''
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

from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, build_opener
import sys

race_label = "//runsc:runsc-race"
install = tmp/"make-runtime"
mode = sys.argv[1]
if mode != "build":
    results=json.loads((out/"result.json").read_text())
mode_target = ["--compilation_mode=fastbuild", "--config=race",
               "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
               "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash"]
try:
    assert platform.machine()=="x86_64"
    if mode=="build":
        source(os.environ["CORRECTED"])
        assert results["corrected"]=="f906c5dd87aeffc3a6f5cec8275b6102a278ea0a"
        assert "--config=race" in Path("tools/bazel.mk").read_text()
        assert "smoke-race-tests: RUNSC_TARGET = $(RACE_FLAGS) //runsc:runsc-race" in Path("Makefile").read_text()
        result,actions=invoke("race","amd64",[race_label],[*mode_target,"--modify_execution_info=^GoLink$=+no-remote-cache"])
        if result["native_exit"]:
            results["verdict"]="RACE_BUILD_FAILED";save();raise SystemExit(1)
        pl="//test/rbe/empty_worker:amd64"
        output_paths=run("bazel","--bazelrc="+str(tmp/"sidecar.bazelrc"),"cquery","--config=x86_64","--host_platform="+pl,"--extra_execution_platforms="+pl,*mode_target,"--modify_execution_info=^GoLink$=+no-remote-cache","--output=files",race_label)
        execroot=Path(run("bazel","--bazelrc="+str(tmp/"sidecar.bazelrc"),"info","execution_root"))
        paths=[execroot/l for l in output_paths.splitlines() if l.startswith("bazel-out/")]
        binary,=[p for p in paths if p.is_file() and p.read_bytes()[:4]==b"\x7fELF"]
        info=elf(binary)
        links=[a for a in actions if a["mnemonic"]=="GoLink" and any(o.get("digest",{}).get("hash")==info["sha256"] and int(o["digest"]["sizeBytes"])==info["bytes"] for o in a["outputs"])]
        link,=links
        props={p["name"]:p["value"] for p in link["platform"]["properties"]}
        assert link["runner"]=="remote" and not link.get("cacheHit") and props["Arch"]=="amd64" and props["container-image"]==image
        flags=link.get("raceFlags",[]);raw=binary.read_bytes()
        marker={s:s.encode() in raw for s in ["runtime.raceinit","ThreadSanitizer","__tsan_init"]}
        results["race_binary"]={**info,"target":race_label,"configured_output":str(binary.relative_to(execroot)),"go_link":link["action"],"exec_properties":props,"link_runner":link["runner"],"cached":bool(link.get("cacheHit")),"link_flags":flags,"markers":marker,"original_scratch_root_control":"https://github.com/tamird/gvisor/actions/runs/41238540967/artifacts/13056457271"}
        assert marker["runtime.raceinit"] and (marker["ThreadSanitizer"] or marker["__tsan_init"])
        install.mkdir()
        shutil.copy2(binary,install/"runsc");assert elf(install/"runsc")==info
        results["verdict"]="RACE_LINK_VERIFIED";save()
    elif mode=="recover":
        assert results["verdict"]=="RACE_LINK_VERIFIED"
        manifest=json.loads((tmp/"ordinary-sidecars.json").read_text())
        assert manifest["source"]==results["corrected"] and manifest["original_run"]==41257521162
        inv=manifest["invocation"]
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self,*args):return None
        recovered={}
        for rel,entry in manifest["files"].items():
            h,n=entry["sha256"],entry["bytes"]
            url="https://app.buildbuddy.io/file/download?"+urlencode({"bytestream_url":"bytestream://remote.buildbuddy.io/blobs/"+h+"/"+str(n),"invocation_id":inv,"filename":Path(rel).name})
            req=Request(url,headers={"x-buildbuddy-api-key":os.environ["BUILDBUDDY_API_KEY"]})
            dest=install/rel;dest.parent.mkdir(exist_ok=True)
            with build_opener(NoRedirect).open(req,timeout=90) as stream,dest.open("wb") as body:shutil.copyfileobj(stream,body)
            dest.chmod(0o755);info=elf(dest);assert info["bytes"]==n and info["sha256"]==h
            recovered[rel]=info
        results["ordinary_release"]={"source":manifest["source"],"invocation":inv,"prior_receipt":manifest["prior_receipt"],"recovered":recovered}
        results["verdict"]="ORIGINAL_RELEASE_FILES_VERIFIED";save()
    else:
        assert mode=="guest" and results["verdict"]=="ORIGINAL_RELEASE_FILES_VERIFIED"
        assert os.getuid()!=0 and "BUILDBUDDY_API_KEY" not in os.environ and not (tmp/"sidecar.bazelrc").exists()
        apparmor=Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
        if apparmor.exists() and apparmor.read_text().strip()!="0":
            subprocess.run(["sudo","sysctl","-w","kernel.apparmor_restrict_unprivileged_userns=0"],check=True)
        subprocess.run(["unshare","-Ur","true"],check=True)
        flags=["--alsologtostderr","--network","none","--debug","--TESTONLY-unsafe-nonroot=true","--rootless","do","true"]
        env={k:os.environ[k] for k in ("HOME","LOGNAME","USER","LANG","PATH","XDG_RUNTIME_DIR") if k in os.environ}
        env["GLIBC_TUNABLES"]="glibc.pthread.rseq=0"
        assert elf(install/"runsc")["sha256"]==results["race_binary"]["sha256"]
        log=tmp/"native-do.log"
        start=now()
        with log.open("w") as f: proc=subprocess.run(["timeout","--kill-after=5s","120s",str(install/"runsc"),*flags],env=env,stdout=f,stderr=subprocess.STDOUT)
        text=log.read_text(errors="replace"); lines=text.splitlines()
        interesting=[l[:700] for l in lines if any(key in l.lower() for key in ("starting container","loading sandbox","changing configuration rootdir","sentry binary","gvisor_sentry","exited with","sandbox exited","creating container","error","fatal","permission denied","threadsanitizer","start root")) and not re.search(r"credential|api.key|authorization|remote.header",l,re.I)]
        results["native_smoke"]={"uid":os.getuid(),"kernel":platform.platform(),"args":flags,"glibc_tunables":env["GLIBC_TUNABLES"],"input_sha256":results["race_binary"]["sha256"],"started":start,"finished":now(),"exit":proc.returncode,"diagnostics":interesting[:12]+interesting[-12:],"log_sha256":sha(log.read_bytes()),"log_lines":len(lines)}
        assert proc.returncode==0 and "Changing configuration RootDir" in text
        results["verdict"]="RACE_CLI_LINKED_ON_SCRATCH_AND_ROOTLESS_GUEST_EXECUTED";save()
except BaseException as err:
    results["phase"]=mode;results["error"]=repr(err)[:800];results["verified_at"]=now();save();raise
results["verified_at"]=now();save()
