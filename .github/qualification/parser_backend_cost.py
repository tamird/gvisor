"""Measure both parser source graphs and execute the candidate's offline fixture."""
import ast
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(os.environ["RUNNER_TEMP"]) / "parser-cost"
PRODUCT = "c2204517cfe4f2c7fd8487c68d1628839a7a8f85"
PARENT = "59b69962bd510505fa8538aa1c4dcc61f615edd3"
SOURCE_SHA = "271da53a2feb0cb221a9276a14268a3b76ad802c7fc8fb1098291a43b06e4c56"
ARCHES = {
    "x86_64": ("amd64", 62, "78d4744ead1f5b19b04d0df558ab182a5300903900755e515b2ca4d10eb2044d"),
    "aarch64": ("arm64", 183, "b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"),
}
BINARY = "//tools/nvidia_driver_differ:driver_ast_parser"
TEST = BINARY + "_test"
EXEC = ["docker", "exec", "gnu"]
BAZEL = [*EXEC, str(ROOT / "bazel"), "--nosystem_rc", "--nohome_rc"]
COMMON = ["--inject_repository=tcp_gnu=" + str(ROOT / "platform"), "--extra_execution_platforms=@tcp_gnu//:gnu",
          "--host_platform=@tcp_gnu//:gnu", "--lockfile_mode=off", "--use_target_platform_for_tests",
          "--noshow_progress", "--curses=no", "--color=no"]

def sha(data):
    return hashlib.sha256(data).hexdigest()

def select(source):
    subprocess.run(["git", "checkout", "--quiet", "--detach", source], check=True)
    assert subprocess.check_output(["git","rev-parse","HEAD"], text=True).strip() == source
    subprocess.run(["git","diff","--quiet","HEAD"],check=True)

def invoke(row, cmd, stem, timeout):
    out,err = (ROOT / (stem+ext) for ext in (".out",".err"))
    with out.open("wb") as a, err.open("wb") as b:
        rc = subprocess.run(["timeout","-k","25",str(timeout),*cmd],stdout=a,stderr=b).returncode
    row["exit"] = rc
    if rc:
        text = (err.read_bytes() + b"\n" + out.read_bytes()[-32768:]).decode(errors="replace")
        hints = []
        for line in text.splitlines():
            if re.search(r"ERROR:|FAILED:|failed to|error downloading|exception:|could not|timed out|missing entry",line,re.I):
                # Public source/target diagnostics only. Never include headers, signed query strings or command echoes.
                if not re.search(r"(?i)api.key|x-buildbuddy|authorization|token|credential|signature=|X-Amz|github_pat_|gh[pousr]_|https?://\S+\?",line):
                    hints.append(line[:480])
        row["errors"] = hints[:12]
        row["stderr_sha256"] = sha(err.read_bytes())
    assert rc == 0, f"{stem} exited {rc}"
    return out

def repo(row, stem):
    out = invoke(row, [*BAZEL,"mod","show_repo","@llvm-project","--lockfile_mode=off"],stem,600)
    txt = out.read_text()
    (ROOT / (stem+".txt")).write_text(txt)
    line = re.search(r"(?m)^\s*targets\s*=\s*(\[[^]]+\]),?\s*$",txt)
    assert line, "no targets in Bazel show_repo output"
    row["backends"] = ast.literal_eval(line[1])
    assert sorted(row["backends"]) == ["AArch64","X86"], row["backends"]
    row["rule"] = re.findall(r"(?m)^(\w+)\($",txt)
    if stem.startswith("candidate"):
        assert "llvm_project_archive" in row["rule"] and SOURCE_SHA in txt
        row["source_archive_sha256"] = SOURCE_SHA

def cost(row,arch,stem):
    out = invoke(row,[*BAZEL,"aquery","--config="+arch,"-c","opt",*COMMON,"--noinclude_artifacts",
                      "--noinclude_commandline","--output=jsonproto",'mnemonic("CppCompile",deps('+BINARY+'))'],stem,1350)
    graph = json.loads(out.read_bytes())
    actions = [a for a in graph["actions"] if a["mnemonic"] == "CppCompile"]
    targets = {str(t["id"]):t["label"] for t in graph["targets"]}
    labels = [targets[str(a["targetId"])] for a in actions]
    repos = Counter(l.split("//",1)[0] or "_main" for l in labels)
    llvm = {k:v for k,v in repos.items() if k.rstrip("/").endswith("llvm-project")}
    assert len(llvm) == 1 and sum(llvm.values()) > 100 and any(l.endswith(BINARY) for l in labels), llvm
    row.update(cpp_compile=len(actions), llvm_cpp_compile=sum(llvm.values()), parser_llvm_repositories=llvm,
               repositories=repos, aquery_sha256=sha(out.read_bytes()))

def runtime(row,arch):
    cpu,machine,image = ARCHES[arch]
    iid=str(uuid.uuid4()); row["invocation"]=iid
    under="/bin/sh -c 'printf \"NATIVE_WORKER_ARCH=%s\\n\" \"$(uname -m)\"; test \"$(uname -m)\" = "+arch+" || exit 125; exec \"$@\"' parser"
    bep=ROOT/(arch+".bep")
    cmd=[*BAZEL,"--bazelrc="+str(ROOT/"private.rc"),"test","--invocation_id="+iid,"--config="+arch,"-c","opt",*COMMON,
         "--jobs=90","--remote_retries=1","--noremote_local_fallback","--strategy=TestRunner=remote",
         "--remote_default_exec_properties=Arch="+cpu,"--remote_default_exec_properties=OSFamily=linux",
         "--remote_default_exec_properties=workload-isolation-type=oci",
         "--remote_default_exec_properties=container-image=docker://mirror.gcr.io/library/ubuntu@sha256:"+image,
         "--nocache_test_results","--flaky_test_attempts=1","--test_sharding_strategy=disabled",
         "--test_arg=-test.v","--test_env=GO_TEST_WRAP_TESTV=1","--run_under="+under,"--remote_download_outputs=toplevel",
         "--remote_download_regex=.*/tools/nvidia_driver_differ/driver_ast_parser$","--build_event_json_file="+str(bep),TEST]
    invoke(row,cmd,"test-"+arch,6100)
    events=(json.loads(l) for l in bep.read_text().splitlines())
    results=[e["testResult"] for e in events if e["id"].get("testResult",{}).get("label")==TEST]
    assert len(results)==1, len(results)
    res=results[0]; info=res["executionInfo"]
    row.update(status=res["status"],strategy=info["strategy"],cached=res.get("cachedLocally",False) or info.get("cachedRemotely",False))
    assert row["status"]=="PASSED" and row["strategy"]=="remote" and not row["cached"], row
    for ext in ("log","xml"):
        raw=subprocess.check_output([*EXEC,"cat","bazel-testlogs/tools/nvidia_driver_differ/driver_ast_parser_test/test."+ext])
        (ROOT/(arch+"-test."+ext)).write_bytes(raw)
        if ext=="log":
            assert ("NATIVE_WORKER_ARCH="+arch).encode() in raw and b"--- PASS: TestParser" in raw
        else:
            assert any(c.get("name","").endswith("TestParser") and not any(k.tag in ("failure","skipped","error") for k in c) for c in ET.fromstring(raw).iter("testcase"))
    probe="import hashlib,struct;from pathlib import Path;b=Path('bazel-bin/tools/nvidia_driver_differ/driver_ast_parser').read_bytes();assert b[:6]==b'\\x7fELF\\x02\\x01';print(str(struct.unpack_from('<H',b,18)[0]),hashlib.sha256(b).hexdigest())"
    em,digest=subprocess.check_output([*EXEC,"python3","-c",probe],text=True).split()
    assert int(em)==machine, em
    row.update(elf_machine=machine,elf_sha256=digest,passed=True)

def license_check(row):
    cmd=[
         "docker","exec","-e","PATH="+str(ROOT)+":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/go/bin",
         "gnu","make","DOCKER_BUILD=false","license-check"]
    out=invoke(row,cmd,"license",900)
    text=out.read_text()+(ROOT/"license.err").read_text()
    m=re.search(r"all (\d+) dependencies have up-to-date license entries",text)
    assert m, "catalog verifier did not report a count"
    row.update(dependencies=int(m[1]),passed=True)

def perform(name, source, action, *args):
    row={"source":source}
    try:
        select(source)
        action(row,*args)
        row["passed"]=True
    except (AssertionError,OSError,KeyError,ValueError,subprocess.SubprocessError) as exc:
        row["error"]=str(exc)[:450]
        print(name, row.get("errors",[]),row["error"],flush=True)
    finally:
        (ROOT/(name+".json")).write_text(json.dumps(row,indent=2)+"\n")
    return row.get("passed",False)

mode=sys.argv[1]
if mode=="graphs":
    ok=[]
    for name,source in (("parent",PARENT),("candidate",PRODUCT)):
        ok.append(perform(name+"-repo",source,repo,name+"-repo"))
        for arch in ARCHES:
            stem=name+"-"+arch
            ok.append(perform(stem,source,cost,arch,stem))
    sys.exit(not all(ok))
if mode=="tests":
    sys.exit(not all([perform("test-"+arch,PRODUCT,runtime,arch) for arch in ARCHES]))
if mode=="license":
    sys.exit(not perform("license",PRODUCT,license_check))
raise ValueError(mode)
