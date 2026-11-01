"""Read the real artifacts from the instrumentor and TableGen builds."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "package-evidence"
out.mkdir()
product = os.environ["PRODUCT"]
subprocess.run(["git", "checkout", "--detach", product], check=True)
bazel = ["bazel", "--bazelrc=" + str(tmp / "sidecar.bazelrc")]
execroot = Path(subprocess.check_output([*bazel, "info", "execution_root"], text=True).strip())
pkg = "//test/rbe/llvm_scratch:"
tools = [pkg+"clang_tool", pkg+"llvm_tool"]
header = "@llvm-project//llvm:instrumentor_variables_gen"
table = ["@llvm-project//clang:parse_attr_gen_filegroup___gen_clang_attr_parser_string_switches_2117484678_genrule",
         "@llvm-project//llvm:acc_gen_impl____gen_directive_impl_234538651_genrule"]
image = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
result = {"source":product,"original_artifact":13072650316,"image":image,"arches":{},"complete":False}
def save():
    (out/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
def sha(data):
    return hashlib.sha256(data).hexdigest()
def opts(arch,musl=False):
    plat="//test/rbe/empty_worker:"+arch+("_musl" if musl else "")
    return ["--config="+{"amd64":"x86_64","arm64":"aarch64"}[arch],"--compilation_mode=fastbuild","--host_platform="+plat,
            "--extra_execution_platforms="+plat,"--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
            "--extra_toolchains=//test/rbe/go_bootstrap:linux_"+arch+"_ash","--remote_accept_cached","--noremote_upload_local_results",
            "--remote_download_outputs=toplevel","--noremote_local_fallback","--spawn_strategy=remote","--jobs=40","--keep_going","--color=no","--curses=no"]
def files(common,label):
    q = subprocess.check_output([*bazel,"cquery",*common,"--output=files",label],text=True,stderr=subprocess.DEVNULL)
    res = [execroot/x for x in q.splitlines() if x.startswith("bazel-out/")]
    if not res: raise RuntimeError("no outputs for " + label)
    return res
def phase(record,arch,name,common,labels):
    log = tmp / (arch+"-"+name+".log")
    bep = tmp / (arch+"-"+name+".bep")
    compact = tmp / (arch+"-"+name+".compact")
    event={"started":time.time()}; record[name]=event; save()
    with log.open("w") as f:
        event["exit"]=subprocess.run([*bazel,"build",*common,"--build_event_json_file="+str(bep),"--execution_log_compact_file="+str(compact),*labels],stdout=f,stderr=f).returncode
    event["wall_seconds"]=round(time.time()-event["started"],1)
    event["diagnostics"]=[l[:430] for l in log.read_text(errors="replace").splitlines() if len(l)<2000 and ("ERROR:" in l or "error:" in l or 'No such file or directory' in l) and not any(s in l.lower() for s in ("header=", "token", "api-key", "authorization", "secret", "credential", "https://"))][:7]
    if bep.exists():
        with bep.open() as f:
            for line in f:
                e=json.loads(line)
                if u:=e.get("started",{}).get("uuid"): event["invocation"]=u
    if compact.exists():
        query='select((.mnemonic=="CppCompile" and (.targetLabel|contains("llvm_scratch:embedded"))) or (.mnemonic=="TdGenerate" and ((.targetLabel|contains("clang_attr_parser_string_switches")) or (.targetLabel|contains("acc_gen_impl_"))))) | {label:.targetLabel,m:.mnemonic,executable:(.commandArgs[0]|split("/")|last),runner:.runner,hit:.cacheHit,exit:(.exitCode//0),props:[.platform.properties[]?|select(.name=="Arch" or .name=="container-image")]}'
        with subprocess.Popen([str(tmp/"bb"),"print","--compact_execution_log="+str(compact)],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as dec:
            data = subprocess.check_output(["jq","-c",query],stdin=dec.stdout,text=True)
            event["actions"]=[json.loads(l) for l in data.splitlines()]
            assert dec.wait()==0,"invalid compact log"
    save()
    if event["exit"]: raise RuntimeError(name+" failed: "+str(event["diagnostics"][:2]))
def elf(path):
    prog = subprocess.check_output(["readelf","-h","-lW","-dW",str(path)],text=True)
    interp = re.findall(r"Requesting program interpreter: ([^\]]+)",prog)
    return {"machine":re.search(r"Machine:\s*(.*)",prog)[1].strip(),"interp":interp,"needed":re.findall(r"\(NEEDED\).*?\[(.*?)\]",prog),"sha256":sha(path.read_bytes()),"bytes":path.stat().st_size}
def selected_action(record,phase_name,mnemonic,minimum,arch):
    actions=record[phase_name].get("actions",[])
    assert len({a["label"] for a in actions if a["m"]==mnemonic and a["exit"]==0 and {"name":"Arch","value":arch} in a["props"] and {"name":"container-image","value":image} in a["props"]})>=minimum,(phase_name,actions)
def archive_repo():
    return execroot/"external/llvm++llvm+llvm-project"
def check_header(row,arch):
    common=opts(arch)
    phase(row,arch,"instrumentor",common,[header,pkg+"embedded"])
    (inc,)=files(common,header)
    folder=archive_repo()/"llvm/include/llvm/Transforms/IPO"
    source=(folder/"InstrumentorRuntimeHelper.h").read_bytes()
    template=(folder/"InstrumentorVariables.inc.in").read_bytes()
    lib=next(x for x in files(common,pkg+"embedded") if x.suffix==".a")
    row["header"]={"size":len(source),"sha256":sha(source),"equals_cmake":inc.read_bytes()==template.replace(b"@LLVM_INSTRUMENTOR_RUNTIME_HELPER@",source),"in_target_object":source+b"\0" in lib.read_bytes(),"generated_sha256":sha(inc.read_bytes())}
    row["archive_has_preconfigured_header"]=(folder/"InstrumentorVariables.inc").exists()
    assert row["header"]["equals_cmake"] and row["header"]["in_target_object"],row["header"]
    selected_action(row,"instrumentor","CppCompile",1,arch)
def check_baseline_tools(row,arch):
    common=opts(arch)
    phase(row,arch,"original_tools",common,tools)
    row["base_elf"]={t:elf(files(common,t)[0]) for t in tools}
    assert all(t["interp"] for t in row["base_elf"].values()),row["base_elf"]
def check_musl_tools(row,arch):
    corrected=opts(arch,musl=True)
    phase(row,arch,"musl_exec",corrected,tools+table)
    row["musl_elf"]={t:elf(files(corrected,t)[0]) for t in tools}
    row["generated"]={t:sha(files(corrected,t)[0].read_bytes()) for t in table}
    assert all(not v["interp"] and not v["needed"] for v in row["musl_elf"].values()),row["musl_elf"]
    selected_action(row,"musl_exec","TdGenerate",2,arch)
def check_local_edit(row):
    root=Path("test/rbe/llvm_scratch_local_edit")
    overlay=root/"utils/bazel/llvm-project-overlay/llvm"
    overlay.mkdir(parents=True)
    (root/"utils/bazel/third_party_build").mkdir()
    helper_path=Path("llvm/include/llvm/Transforms/IPO/InstrumentorRuntimeHelper.h")
    templ_path=helper_path.with_name("InstrumentorVariables.inc.in")
    repo=archive_repo()
    for path in [helper_path,templ_path,Path("llvm/CMakeLists.txt"),Path("cmake/Modules/LLVMVersion.cmake")]:
        (root/path).parent.mkdir(exist_ok=True,parents=True)
        shutil.copyfile(repo/path,root/path)
    original_build=(repo/"llvm/BUILD.bazel").read_text()
    block=re.search(r'^expand_template\(\n    name = "instrumentor_variables_gen",[^)]*?^\)',original_build,re.M|re.S)
    assert block
    (overlay/"BUILD.bazel").write_text('load("@bazel_skylib//rules:expand_template.bzl", "expand_template")\nload("//:vars.bzl", "LLVM_INSTRUMENTOR_RUNTIME_HELPER")\n'+block[0]+'\n')
    with Path("MODULE.bazel").open("a") as f:
        f.write('\nllvm_local_probe = use_repo_rule("@llvm//private:llvm_project_from_path.bzl", "llvm_project_from_path")\nllvm_local_probe(name="llvm_local_edit",path="test/rbe/llvm_scratch_local_edit",targets=["X86"])\n')
    label="@llvm_local_edit//llvm:instrumentor_variables_gen"
    common=opts("amd64")
    helper=root/helper_path
    for step in ["before","after"]:
        if step=="after":
            with helper.open("ab") as f: f.write(b'\n// Local edit with a literal \\n escape.\n')
        phase(row,"local-edit",step,common,[label])
        actual=files(common,label)[0].read_bytes()
        expected=(root/templ_path).read_bytes().replace(b"@LLVM_INSTRUMENTOR_RUNTIME_HELPER@",helper.read_bytes())
        row[step]["source"]=sha(helper.read_bytes());row[step]["generated"]=sha(actual)
        assert actual==expected,"local helper edit was not reflected in configured header"
    assert row["before"]["generated"]!=row["after"]["generated"]
for arch in ("amd64","arm64"):
    row=result["arches"][arch]={"failures":{}}
    for check in (check_header,check_baseline_tools,check_musl_tools):
        try: check(row,arch)
        except Exception as exc: row["failures"][check.__name__]=repr(exc)[:650]
        save()
    row["complete"]=not row["failures"]
    print(arch,row["complete"],row["failures"],flush=True)
    save()
row=result["local_edit"]={}
try:
    check_local_edit(row)
    row["complete"]=True
except Exception as exc:
    row["failed"]=repr(exc)[:650]
print("local input invalidation",row.get("complete",False),row.get("failed",""),flush=True)
result["complete"]=all(r.get("complete",False) for r in result["arches"].values()) and row.get("complete",False)
save()
raise SystemExit(not result["complete"])
