"""Check the real protoc authenticity validation using the selected Linux toolchains."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "sidecar-evidence"
OUT.mkdir(exist_ok=True)
MOD = Path("MODULE.bazel")
ORIGINAL = MOD.read_text()
IMAGE = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
OWNER = "@com_google_protobuf//bazel/private/oss/toolchains/prebuilt:authenticity_validation"
results = []
def check(arch, mode, patch, permissive=False):
    tag = arch + "-" + mode
    folder = OUT / tag
    folder.mkdir()
    log, compact, bep = [TMP / (tag + x) for x in (".log", ".binpb", ".jsonl")]
    MOD.write_text(ORIGINAL.replace("validation-stock.patch", "validation-"+patch+".patch"))
    platform = "//test/rbe/native_sidecars:" + arch
    command = ["bazel", "--bazelrc="+str(TMP/"sidecar.bazelrc"), "build", "--config="+{"amd64":"x86_64","arm64":"aarch64"}[arch],
               "--extra_toolchains=//test/rbe/go_bootstrap:linux_"+arch+"_ash", "--host_platform="+platform, "--extra_execution_platforms="+platform,
               "--remote_download_outputs=all", "--noremote_accept_cached", "--color=no", "--curses=no",
               "--execution_log_compact_file="+str(compact), "--build_event_json_file="+str(bep), "--nobuild_event_json_file_path_conversion",
               "--output_groups=_validation", "--@com_google_protobuf//bazel/flags:allow_nonstandard_protoc="+str(permissive).lower(), OWNER]
    if mode=="legacy":
        command = [v for v in command if not v.startswith("--extra_toolchains=")]
        command.insert(-1,"--noincompatible_enable_proto_toolchain_resolution")
    with log.open("w") as handle:
        rc=subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT).returncode
    text=log.read_text(errors="replace")
    record={"arch":arch,"mode":mode,"exit":rc,"source":os.environ["GITHUB_SHA"],"expectedSource":Path("test/rbe/protoc_validation/source.sha").read_text().strip()}
    record["invocation"]=next((json.loads(l)["started"]["uuid"] for l in bep.read_text().splitlines() if '"started"' in l and "started" in json.loads(l)),None) if bep.exists() else None
    record["diagnostics"]=[l[-580:] for l in text.splitlines() if "api-key" not in l.lower() and re.search(r"ERROR:|Actual:|Expected:|does not match|No such file|not found|failed to start|invalid|ERROR ", l,re.I)][-15:]
    try:
        raw=subprocess.run([str(TMP/"bb"),"print","--compact_execution_log="+str(compact)],stdout=subprocess.PIPE,check=True)
        j=subprocess.run(["jq","-c",'select(.mnemonic == "ProtocAuthenticityCheck")'],input=raw.stdout,stdout=subprocess.PIPE,check=True)
        records=[json.loads(l) for l in j.stdout.splitlines()]
        if mode=="legacy":
            assert rc==0 and not records and record["invocation"],record
            record.update(verified=True,validationActions=len(records))
            return
        assert len(records)==1, ("validation actions",len(records))
        act=records[0];args=act.get("commandArgs",[]);props={p["name"]:p["value"] for p in act["platform"]["properties"]}
        record["action"]={"executable":args[0],"runner":act.get("runner"),"exit":act.get("exitCode",0),"cacheHit":act.get("cacheHit",False),"arch":props.get("Arch"),"image":props.get("container-image")}
        record["action"]["protoc"]=next((v for v in args if v.endswith("/protoc")),None)
        assert not act.get("cacheHit") and props["Arch"]==arch and props["container-image"]==IMAGE and act["runner"]=="remote", record["action"]
        if mode=="upstream":
            assert rc and args[0] in ["/bin/bash","/usr/bin/bash","/bin/sh"] and ("No such file" in text or "not found" in text or "no such file" in text),record
        else:
            assert args[0].endswith("/ash"),args[:1]
            assert (any(v.endswith("/protoc") for v in args)), args[-3:]
            if mode=="strict-mismatch":
                assert rc and "protoc version does not match" in text and "libprotoc 35.1" in text
            else:
                assert rc==0,record["diagnostics"]
                out=next(v for v in act["actualOutputs"] if v["path"].endswith("validation_output.txt"))
                contents=(Path("bazel-bin")/out["path"].split("/bin/",1)[1]).read_bytes()
                assert contents==b"libprotoc 35.1\n" and hashlib.sha256(contents).hexdigest()==out["digest"]["hash"],contents
                record.update(version=contents.decode().strip(),outputSha256=out["digest"]["hash"])
        record["verified"]=True
    except Exception as e:
        record["verificationError"]=str(e)[:670]
        record["verified"]=False
    finally:
        results.append(record)
        (folder/"result.json").write_text(json.dumps(record,indent=2)+"\n")
        print(tag,json.dumps(record),flush=True)
try:
    check("amd64","upstream","stock")
    for arch in ("amd64","arm64"):
        check(arch,"matched","candidate")
    check("amd64","strict-mismatch","mismatch")
    check("amd64","allowed-mismatch","mismatch",True)
    check("amd64","legacy","candidate")
finally:
    MOD.write_text(ORIGINAL)
assert all(r["verified"] for r in results) and len(results)==6
