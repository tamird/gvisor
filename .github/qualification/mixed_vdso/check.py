"""Compare two native gVisor vDSOs using original mixed RBE actions."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlparse
import uuid
import exec_cas as cas

W=Path(os.environ["RUNNER_TEMP"])/"mixed-vdso"
S="46f87ffda9209c9f284898bf4eb3e091f5b35ceb"
mode,phase=sys.argv[1:]
P={"stock":"source-presubmit-mixed","mapped":"source-presubmit-mixed-llvm-mapped"}[mode]
T={"//vdso:mixed_abi_test_amd64":("amd64","x86_64",62),"//vdso:mixed_abi_test_arm64":("arm64","aarch64",183)}
OWNER="//vdso:vdso.so"
H=re.compile(r"[a-f0-9]{64}\Z")
IMAGE="docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==S
out=W/"results"/(mode+".json")
r=json.loads(out.read_text()) if out.exists() else {"source":S,"mode":mode}
def save():
    s=json.dumps(r,indent=1,sort_keys=True);assert len(s)<350000;out.write_text(s+"\n")

def bazel(name,args,minutes,remote=False):
    cmd=[str(W/"bazel"),"--nosystem_rc","--nohome_rc","--host_jvm_args=-Xmx7g"]
    if remote:cmd+=["--bazelrc="+str(W/"remote.rc")]
    cmd += [name,"--config="+P,"--experimental_output_paths=strip","--incompatible_modify_execution_info_additive","--color=no",*args]
    log=W/(mode+name+".log")
    with (W/(mode+name+".out")).open("wb") as stdout,log.open("wb") as stderr:
        try:n=subprocess.run(cmd,stdout=stdout,stderr=stderr,timeout=minutes*60).returncode
        except subprocess.TimeoutExpired:n=124
    r[name+"_exit"]=n
    if n:
        text=log.read_text(errors="replace")
        r[name+"_signals"]=[s for s in ("Traceback","no matching","no such","conflict","analysis failed","compilation failed","permission denied","timeout","Error in","mapping","exec format error") if s.lower() in text.lower()]
        if not remote:r["analysis_first_errors"]=[l[:450] for l in text.splitlines() if l.startswith("ERROR:")][:5]
    save();return n

def analyze():
    (W/"targets").write_text("\n".join(T)+"\n")
    expression='str(target.label)+" "+str(build_options(target).get("//command_line_option:platforms"))+" "+str(build_options(target).get("//command_line_option:cpu"))'
    if not bazel("cquery",["--spawn_strategy=local","--strategy=TestRunner=local","--jobs=4","--output=starlark","--starlark:expr="+expression,"set("+" ".join(T)+")"],11):
        rows=(W/(mode+"cquery.out")).read_text().splitlines()
        r["outer_configured"]={t:{"expected_cpu_and_platform":any(t in row and "//tools/bazeldefs:linux_"+cpu in row and row.endswith(" "+("k8" if cpu=="amd64" else "aarch64")) for row in rows)} for t,(cpu,_,_) in T.items()}
        save()
    q='let roots = set('+' '.join(T)+') in mnemonic("^TestRunner$",$roots) union mnemonic("^(CppCompile|CppLink)$", filter("//vdso:vdso.so$",deps($roots)))'
    if bazel("aquery",["--spawn_strategy=local","--strategy=TestRunner=local","--jobs=4","--output=jsonproto","--noinclude_commandline","--noinclude_artifacts",q],11):return
    g=json.loads((W/(mode+"aquery.out")).read_bytes());names={str(t["id"]):t["label"].removeprefix("@@") for t in g["targets"]}
    r["tests_selected"]={};r["configured"]=[]
    for a in g["actions"]:
        label=names[str(a["targetId"])];p=a.get("executionPlatform","").removeprefix("@@")
        if label in T and a["mnemonic"]=="TestRunner":r["tests_selected"][label]=p
        elif label==OWNER:r["configured"].append([a["mnemonic"],p,any(k["key"]=="supports-path-mapping" for k in a.get("executionInfo",[]))])
    c=[v for v in r["configured"] if v[0]=="CppCompile"]
    r["eligible"]=(r["tests_selected"]=={t:"//test/rbe:ubuntu_"+a for t,(a,_,_) in T.items()} and len(c)>=4 and all(v[2]==(mode=="mapped") for v in c) and sum(v[0]=="CppLink" for v in r["configured"])>=2)
    save()

def execute():
    assert r["eligible"]
    bazel("test",["--target_pattern_file="+str(W/"targets"),"--keep_going","--jobs=28","--nocache_test_results","--flaky_test_attempts=1","--nouse_action_cache","--modify_execution_info=^TestRunner$=+no-remote-cache",
        "--remote_download_outputs=minimal","--remote_download_regex=.*vdso/mixed_abi_test_(amd64|arm64)/test[.](log|xml)","--execution_log_sort=false","--execution_log_json_file="+str(W/(mode+".spawns")),"--build_event_json_file="+str(W/(mode+".bep"))],32,True)
def json_entries(path):
    # Bazel writes a sequence of multiline protobuf JSON objects.
    dec=json.JSONDecoder()
    with path.open() as stream:
        chunk=""
        while True:
            chunk=chunk.lstrip()
            if not chunk:
                chunk=stream.read(65536)
                if not chunk:return
                continue
            try: entry,end=dec.raw_decode(chunk)
            except json.JSONDecodeError:
                extra=stream.read(65536)
                if not extra:raise
                chunk+=extra;continue
            yield entry
            chunk=chunk[end:]

def inputs(files):
    rows=[]
    for f in files:
        p=f.get("path","");h=f.get("digest",{}).get("hash","")
        if "/vdso/" in p and p.endswith((".so",".o")) and H.fullmatch(h) and re.fullmatch(r"[A-Za-z0-9_+./@=\-]{1,390}",p):
            rows.append([p,h])
    return rows

def marker(label,item):
    uri=urlparse(item.get("uri",""));m=re.search(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/(\d+)$",uri.path)
    h,n=(m[1],int(m[2])) if m else (item.get("digest"),int(item.get("length",0)))
    if not H.fullmatch(h or "") or n>512000:return {}
    paths=[Path(unquote(uri.path))] if uri.scheme=="file" else []
    paths+=list(Path("bazel-out").glob("*/testlogs/"+label[2:].replace(":","/")+"/test.log"))
    for p in paths:
        if not p.is_file():continue
        b=p.read_bytes()
        if (len(b),hashlib.sha256(b).hexdigest())!=(n,h):continue
        lines=b.decode(errors="replace").splitlines()
        rows=[json.loads(l.removeprefix("GVISOR_VDSO ")) for l in lines if l.startswith("GVISOR_VDSO ")]
        row={"log_sha256":h,"markers":len(rows),"diagnostics":[x for x in ("AssertionError","Traceback","PermissionError","No such file","Segmentation fault","symbol lookup error","CalledProcessError","CRITICAL") if any(x in l for l in lines)]}
        if len(rows)==1:
            v=rows[0];_,machine,num=T[label]
            wanted={"__kernel_rt_sigreturn","__kernel_clock_getres","__kernel_clock_gettime","__kernel_gettimeofday"} if num==183 else {"__kernel_rt_sigreturn",*(n for s in ("clock_gettime","gettimeofday","getcpu","time") for n in (s,"__vdso_"+s))}
            if (v["sha256"]==("7e34fe3c975a09c00f3486e7c9a447493b142e7aa02f989e4222d6cb39e7077a" if num==183 else "b19622c197737dc9086d5d0314768403dfe9c8123ba03b0d20fd01df14a4db45") and v["arch"]==machine and v["machine"]==num and v["version"]==("LINUX_2.6.39" if num==183 else "LINUX_2.6")
                and v["checked"] and set(v["symbols"])==wanted and set(v["symbol_offsets"])==wanted and all(0<=o<2**23 for o in v["symbol_offsets"].values())
                and H.fullmatch(v["sha256"]) and H.fullmatch(v["load_sha256"]) and v["called"] in wanted):
                row["elf"]={k:v[k] for k in ("arch","machine","version","size","sha256","load_sha256","called","symbol_offsets")}
        return row
    return {}

def collect():
    r["tests"]={};r["spawns"]=[];save();bep=W/(mode+".bep")
    if not bep.exists():return
    for line in bep.open():
        e=json.loads(line);label=e["id"].get("testResult",{}).get("label")
        if "started" in e:r["invocation"]=str(uuid.UUID(e["started"]["uuid"]))
        if label not in T or "testResult" not in e:continue
        t=e["testResult"];info=t.get("executionInfo",{})
        v={"status":t.get("status"),"remote":info.get("strategy")=="remote","fresh":not(t.get("cachedLocally") or info.get("cachedRemotely"))}
        r["tests"].setdefault(label,[]).append(v);save()
        for item in t.get("testActionOutput",[]):
            if item.get("name")=="test.log":v.update(marker(label,item))
        save()
    log=W/(mode+".spawns")
    if not log.exists():return
    for e in json_entries(log):
        label=e.get("targetLabel","").removeprefix("@@");mn=e.get("mnemonic","")
        if not ((label==OWNER and mn in ("CppCompile","CppLink")) or (label in T and mn=="TestRunner")):continue
        if mn=="TestRunner" and not any(a.endswith("/test-setup.sh") for a in e.get("commandArgs",[])):continue
        d=e.get("digest",{});h=d.get("hash");n=int(d.get("sizeBytes",0))
        if not H.fullmatch(h or "") or not(0<n<=131072):continue
        runner=e.get("runner","")
        row={"owner":label,"mnemonic":mn,"runner":runner if runner in ("remote","remote cache hit","disk cache hit") else "other","cache_hit":bool(e.get("cacheHit")),"action":h,"size":n,
             "inputs":inputs(e.get("inputs",[])) if mn=="CppLink" else [],"outputs":inputs(e.get("actualOutputs",[])) if mn!="TestRunner" else []}
        r["spawns"].append(row);assert len(r["spawns"])<=36;save()
    for label,trs in r["tests"].items():
        if len(trs)!=1 or "elf" not in trs[0]:continue
        v=trs[0];h=v["elf"]["sha256"]
        links=[a for a in r["spawns"] if a["mnemonic"]=="CppLink" and any(x[1]==h for x in a["outputs"])]
        v["links"]=[a["action"] for a in links]
        src={x[1] for a in links for x in a["inputs"]}
        v["compiles"]=[a["action"] for a in r["spawns"] if a["mnemonic"]=="CppCompile" and any(x[1] in src for x in a["outputs"])]
    save()

def platform(raw):
    props={}
    for tag,v in cas.fields(raw):
        if tag==1:
            kv=dict(cas.fields(v));props[kv[1].decode()]=kv[2].decode()
    cpu=props.get("Arch")
    return {"cpu":cpu if cpu in ("amd64","arm64") else None,"ubuntu":props.get("container-image")==IMAGE,
            "linux":props.get("OSFamily")=="linux","oci":props.get("workload-isolation-type")=="oci"}

def physical():
    cas.INV=str(uuid.UUID(r["invocation"]))
    assert 0<len(r["spawns"])<=36
    r["actions"]={}
    for a in r["spawns"]:
        label=a["owner"];mn=a["mnemonic"]
        assert ((label==OWNER and mn in ("CppCompile","CppLink")) or (label in T and mn=="TestRunner"))
        h=a["action"];assert H.fullmatch(h) and 0<a["size"]<=131072
        try:
            ac=dict(cas.fields(cas.blob({"hash":h,"sizeBytes":a["size"]},131072)));cd=cas.digest_msg(ac[1]);rd=cas.digest_msg(ac[2]);cmd=list(cas.fields(cas.blob(cd,3*1024*1024)))
            p=platform(ac[10]) if 10 in ac else next((platform(v) for t,v in cmd if t==5),{})
            args=[v.decode() for t,v in cmd if t==1]
            declared=[v.decode() for t,v in cmd if t in (3,7)]
            r["actions"][h]={"command":cd["hash"],"input_root":rd["hash"],"platform":p,"qemu":any("qemu-" in arg for arg in args),
                "mapping":any("bazel-out/cfg/" in v for v in declared),"tool":Path(args[0]).name if args and re.fullmatch(r"[A-Za-z0-9._-]{1,64}",Path(args[0]).name) else None}
        except (OSError,KeyError,IndexError,AssertionError,ValueError) as exc:r["actions"][h]={"diagnostic":type(exc).__name__}
        save()
    def checked(a):
        y=r["actions"].get(a["action"],{});p=y.get("platform",{})
        return a["runner"] in ("remote","remote cache hit") and p.get("linux") and p.get("ubuntu") and p.get("oci") and p.get("cpu") and not y.get("qemu")
    for label,vv in r["tests"].items():
        if len(vv)!=1:continue
        natives=[a for a in r["spawns"] if a["owner"]==label and a["mnemonic"]=="TestRunner" and checked(a)]
        vv[0]["native_runner"]=len(natives)==1 and not natives[0]["cache_hit"] and r["actions"][natives[0]["action"]]["platform"]["cpu"]==T[label][0]
    ok={a["action"] for a in r["spawns"] if checked(a)}
    r["verified"]=(r["test_exit"]==0 and set(r["tests"])==T.keys() and all(len(vv)==1 and vv[0].get("elf") and vv[0].get("native_runner") and vv[0]["fresh"] and vv[0]["remote"] and vv[0]["status"]=="PASSED" and len(vv[0].get("links",[]))==1 and len(set(vv[0].get("compiles",[])))>=2 and set(vv[0]["links"]+vv[0]["compiles"])<=ok for vv in r["tests"].values()))
    save()
try:{"analyze":analyze,"execute":execute,"collect":collect,"physical":physical}[phase]()
except Exception as exc:
    r[phase+"_error"]=type(exc).__name__
    save()
    raise
