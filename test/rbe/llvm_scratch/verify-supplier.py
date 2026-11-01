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
os.chdir(Path(os.environ["GITHUB_WORKSPACE"])/"supplier")
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==product
record={"source":product}
(out/"verdict.json").write_text(json.dumps(record))
fixture=Path("test/rbe/llvm_scratch")
fixture.mkdir(parents=True)
fixture_build='''load("@rules_cc//cc:cc_library.bzl", "cc_library")

cc_library(
    name = "embedded",
    srcs = ["embedded.cc"],
    additional_compiler_inputs = ["@llvm-project//llvm:instrumentor_variables_gen"],
    hdrs = ["@llvm-project//llvm:instrumentor_variables_gen"],
    copts = ["-include", "$(execpath @llvm-project//llvm:instrumentor_variables_gen)"],
    linkstatic = True,
)
'''
for arch,cpu in [("amd64","x86_64"),("arm64","aarch64")]:
    fixture_build+=f'''
platform(
    name="{arch}",
    constraint_values=["@platforms//os:linux", "@platforms//cpu:{cpu}"],
)

platform(
    name="{arch}_exec",
    parents=[":{arch}"],
    constraint_values=["//constraints/libc:musl"],
    exec_properties={{"Arch":"{arch}", "OSFamily":"linux", "workload-isolation-type":"oci", "container-image":"docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"}},
)
'''
(fixture/"BUILD.bazel").write_text(fixture_build)
(fixture/"embedded.cc").write_text('extern "C" const char *const llvm_instrumentor_source = InstrumentorRuntimeHelper;\n')
bazel = ["bazel", "--bazelrc=" + str(tmp / "sidecar.bazelrc")]
execroot = Path(subprocess.check_output([*bazel, "info", "execution_root"], text=True).strip())
outputbase=Path(subprocess.check_output([*bazel,"info","output_base"],text=True).strip())
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
    plat=pkg+arch
    return ["--platforms="+plat,"--host_platform="+plat+"_exec","--extra_execution_platforms="+plat+"_exec","--extra_toolchains=//toolchain:all","--compilation_mode=fastbuild",
            "--remote_accept_cached","--noremote_upload_local_results","--remote_download_outputs=toplevel","--noremote_local_fallback","--spawn_strategy=remote","--jobs=30","--keep_going","--color=no","--curses=no"]
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
    return next(p for p in outputbase.joinpath("external").glob("*+llvm+llvm-project") if p.is_dir())
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
    (overlay/"BUILD.bazel").write_text('load("@bazel_skylib//rules:expand_template.bzl", "expand_template")\nload(":instrumentor.bzl", "INSTRUMENTOR_RUNTIME_HELPER")\n'+block[0]+'\n')
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
for arch in ["amd64","arm64"]:
    row=result["arches"][arch]={"failures":{}}
    options=opts(arch)
    try:
        phase(row,arch,"header",options,[header])
        generated=files(options,header)[0].read_bytes()
        folder=archive_repo()/"llvm/include/llvm/Transforms/IPO"
        helper=(folder/"InstrumentorRuntimeHelper.h").read_bytes()
        expected=(folder/"InstrumentorVariables.inc.in").read_bytes().replace(b"@LLVM_INSTRUMENTOR_RUNTIME_HELPER@",helper)
        row["bytes"]={"input":len(helper),"generated":sha(generated),"equals_cmake":generated==expected}
        assert generated==expected
    except Exception as e:row["failures"]["header"]=repr(e)[:650]
    save()
    try:
        phase(row,arch,"consumer",options,[pkg+"embedded"])
        lib=next(x for x in files(options,pkg+"embedded") if x.suffix==".a")
        embedded=(archive_repo()/"llvm/include/llvm/Transforms/IPO/InstrumentorRuntimeHelper.h").read_bytes()+b"\0"
        row["contains_helper"]=embedded in lib.read_bytes()
        assert row["contains_helper"]
        selected_action(row,"consumer","CppCompile",1,arch)
    except Exception as e:row["failures"]["consumer"]=repr(e)[:650]
    row["complete"]=not row["failures"]
    save()
row=result["local_edit"]={}
try:
    check_local_edit(row)
    row["complete"]=True
except Exception as e:row["failed"]=repr(e)[:650]
result["complete"]=all(r["complete"] for r in result["arches"].values()) and row.get("complete",False)
save()
for arch,row in result["arches"].items():print(arch,row["failures"])
print("local",result["local_edit"].get("failed",result["local_edit"].get("complete")))
raise SystemExit(not result["complete"])
