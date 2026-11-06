import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import traceback
import uuid

work = Path(os.environ["RUNNER_TEMP"]) / "writer-runtime"
public_source = "bb983b988802b854bd03dd7a0a5220e7e7b4440e"
platforms = {
    "x86_64": ("aarch64", "amd64", "d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"),
    "aarch64": ("x86_64", "arm64", "b0aca6ddfad947e66782a29534647936d4d645a1b96d9c8a97e4f53b1607d428"),
}
cases = {
    "//shim:config": "shim/config.tar",
    "//test/rbe/pkg_writer:compressed_shim": "test/rbe/pkg_writer/compressed_shim.tar.bz2",
}
report = {"selection_status": "VERIFIED_13490342566", "source": public_source, "cases": {}}

def store():
    (work / "selection.json").write_text(json.dumps(report, indent=2) + "\n")

def run_case(target, output, arch, cross_arch, remote_arch, image):
    label = arch + "-" + output.rsplit("/", 1)[1]
    rec = report["cases"][label] = {"expected_owner": target, "invocation": str(uuid.uuid4())}
    store()
    events = work / (label + ".bep")
    execution = work / (label + ".execution")
    stderr = work / (label + ".stderr")
    exec_platform = "@llvm//platforms:linux_" + arch + "_musl"
    with stderr.open("wb") as err:
        process = subprocess.run([
            "timeout", "-k", "15", "780", str(work / "bazel"), "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(work / "private.rc"),
            "build", "--invocation_id=" + rec["invocation"], "--config=" + cross_arch, "-c", "opt", "--lockfile_mode=off",
            "--noshow_progress", "--color=no", "--curses=no", "--extra_execution_platforms=" + exec_platform, "--host_platform=" + exec_platform,
            "--platforms=@llvm//platforms:linux_" + cross_arch + "_glibc_static", "--@rules_python//python/config_settings:bootstrap_impl=native",
            "--remote_default_exec_properties=Arch=" + remote_arch, "--remote_default_exec_properties=OSFamily=linux",
            "--remote_default_exec_properties=workload-isolation-type=oci", "--remote_default_exec_properties=recycle-runner=false",
            "--remote_default_exec_properties=container-image=docker://gcr.io/distroless/static-debian13@sha256:" + image,
            "--modify_execution_info=^PackageTar$=+no-remote-cache", "--execution_log_json_file=" + str(execution),
            "--build_event_json_file=" + str(events), "--remote_download_outputs=toplevel", target,
        ], stdout=subprocess.DEVNULL,stderr=err)
    rec["exit"] = process.returncode
    diagnostics = stderr.read_text(errors="replace")
    rec["error_categories"] = [s for s in ("no such package", "no such target", "no matching toolchain", "permission denied", "failed to fetch", "error downloading", "no such file", "not a valid", "exec format error", "unrecognized option") if s in diagnostics.lower()]
    rec["missing_packages"] = sorted(set(re.findall(r"no such package '([A-Za-z0-9_+./~@:-]{1,220})'", diagnostics)))[:3]
    rec["logs"] = []
    if events.is_file():
        for line in events.open():
            event = json.loads(line)
            action = event.get("action", {})
            if action.get("success") is False:
                m = re.search(r"/blobs/([0-9a-f]{64})/(\d+)$",action.get("stderr",{}).get("uri",""))
                if m and len(rec["logs"]) < 3:
                    rec["logs"].append({"type":action.get("type",""),"digest":m[1],"size":int(m[2])})
            for item in event.get("namedSetOfFiles",{}).get("files",[]):
                if item.get("name","").endswith(output) and re.fullmatch(r"[0-9a-f]{64}",item.get("digest","")):
                    rec["bep_sha256"]=item["digest"]
    store()
    if process.returncode:
        return
    archive=Path("bazel-bin")/output
    raw=archive.read_bytes()
    rec["archive_sha256"]=hashlib.sha256(raw).hexdigest()
    rec["is_bzip2"]=raw.startswith(b"BZh")
    with tarfile.open(archive, "r:bz2" if output.endswith(".bz2") else "r:") as tar:
        members=[m for m in tar if m.isfile()]
        assert len(members)==1
        payload=tar.extractfile(members[0]).read()
        rec["member"]={"name":members[0].name,"mode":members[0].mode,"sha256":hashlib.sha256(payload).hexdigest()}
    store()
    jq='[.[]|if type=="array" then .[] else . end|select(.mnemonic=="PackageTar")|{owner:.targetLabel,runner,exit:(.exitCode//0),cached:(.cacheHit//false),props:((.platform.properties//[])|map({key:.name,value:.value})|from_entries),writer:.commandArgs[0]}]'
    actions=json.loads(subprocess.check_output(["jq","-sc",jq,execution]))
    rec["actions"]=[{"owner":a["owner"],"runner":a["runner"],"exit":a["exit"],"cached":a["cached"],"arch":a["props"].get("Arch"),"image":a["props"].get("container-image")} for a in actions]
    store()
    executed,=[a for a in actions if a["owner"] in (target,"@@"+target)]
    assert executed["runner"]=="remote" and not executed["cached"] and executed["exit"]==0
    assert executed["props"].get("Arch")==remote_arch and executed["props"].get("container-image","").endswith(image)
    assert executed["writer"].endswith("/pkg/private/tar/build_tar")
    assert rec["is_bzip2"]==output.endswith(".bz2")
    assert rec["member"]["name"].lstrip("./")=="etc/containerd/runsc.toml" and rec["member"]["mode"]&0o7777==0o644
    assert payload==Path("shim/runsc.toml").read_bytes()
    rec["passed"]=True
    store()

allowed={".github/qualification/package_writers.py",".github/qualification/writer_runtime.py",".github/qualification/writer_runtime.cquery",".github/workflows/build.yml","test/rbe/pkg_writer/BUILD"}
changed=set(subprocess.check_output(["git","diff","--name-only",public_source,"HEAD"],text=True).splitlines())
assert changed==allowed,changed
for arch,(cross_arch,remote_arch,image) in platforms.items():
    for target,output in cases.items():
        try:
            run_case(target,output,arch,cross_arch,remote_arch,image)
        except (OSError,ValueError,KeyError,IndexError,AssertionError,subprocess.CalledProcessError) as err:
            key=arch+"-"+output.rsplit("/",1)[1]
            report["cases"][key]["report_error"]={"type":type(err).__name__,"line":traceback.extract_tb(err.__traceback__)[-1].lineno}
            store()
assert len(report["cases"])==4 and all(v.get("passed") for v in report["cases"].values())
