"""Qualify the public race executables and their existing analysis rules."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree

SOURCE = "3dab3494e2a2154244e8b2fc77699c02c2277a56"
ROOTS = ("//runsc:runsc-race", "//runsc/cmd/sentry:gvisor_sentry-race")
NOGO = tuple(x + "_nogo" for x in ROOTS)
POLICY = ("//runsc:runsc_deps_test", "//runsc/cmd/sentry:gvisor_sentry_deps_test")
DIR = Path(os.environ["CHECK_DIR"])
PUBLIC = DIR / "public"
BASE = ["-c", "opt", "--jobs=4", "--remote_cache=", "--remote_executor=", "--disk_cache="]
RACE = [*BASE, "--config=race"]
LINES = ("no matching", "not available", "not declared", "No attribute", "no such",
         "not visible", "does not refer", "error loading", "analysis failed", "cannot parse",
         "unable to find library", "exceeded", "not found", "error executing", "visibility",
         "invalid", "does not match", "has no attribute", "unknown", "not a valid", "undefined")

def sha(b):
    return hashlib.sha256(b).hexdigest()

def sig(p):
    return {"bytes":p.stat().st_size,"sha256":sha(p.read_bytes())}

def save():
    (PUBLIC / (mode + ".json")).write_text(json.dumps(r,indent=2))

def command(name, argv, cap, require=True):
    out,err=DIR/(name+".out"),DIR/(name+".err")
    with out.open("wb") as a,err.open("wb") as b:
        proc=subprocess.run(["timeout","-k","15",str(cap),*argv],stdout=a,stderr=b)
    tail=err.read_bytes()[-65536:].decode(errors="replace")
    r["commands"][name]={"exit":proc.returncode,"stdout_bytes":out.stat().st_size,
        "stderr_sha256":sig(err)["sha256"],"hints":[x for x in LINES if x.lower() in tail.lower()]}
    if proc.returncode:
        for line in tail.splitlines():
            if not (line.startswith("ERROR:") or "Error in " in line or "No attribute " in line):continue
            line=re.sub(r"/(?:home|tmp|opt|usr|var)/[^ ]+","[path]",line)
            if len(line)<1200 and all(ord(c)<128 for c in line) and not any(t in line.lower() for t in ("token","authorization","password","http","credential","secret","environment")):
                r["commands"][name]["first_error"]=line[:450];break
    save()
    if require and proc.returncode:raise ValueError(name)
    return out

def evaluate(s, name, flags, expr):
    return command(name,["bazel","cquery",*flags,"--output=starlark","--starlark:expr="+s,expr],300).read_text()

def local(l):
    return "//"+l.split("//",1)[-1]

def norm(name):
    return name.lstrip("/").replace(":","/")

def selector(name,flags,probe=False):
    labels=("//runsc/race_probe:source",) if probe else ROOTS
    expr='filter("(_impl|_musl|/raw[.]txt|/musl[.]txt|:raw[.]txt|:musl[.]txt)$", deps(set(%s), 2))' % " ".join(labels)
    s='"%s\\t%s" % (target.label, build_options(target).get("//command_line_option:platforms", []))'
    if probe:s='str(target.label)+"\\t-"'
    rows=[]
    for line in evaluate(s,name,flags,expr).splitlines():
        label,plat=line.split("\t");label=local(label)
        if probe:
            if label.startswith("//runsc/race_probe:"):rows.append((label,"-"))
        elif label in {l+suffix for l in ROOTS for suffix in ("_musl","_impl")}:
            assert len(plat)<256 and re.fullmatch(r"""[a-zA-Z0-9@:._/+\[\] ,'"()-]*""",plat)
            rows.append((label,plat))
    r["selections"][name]=rows;save()
    return rows

def check_selection():
    r["selections"]={};r["selection_checks"]={}
    r["prior_selection"]={"source":"629d27e5ff9478b7d8d60f8245da07f275373170",
        "artifact":13456817320,"zip_sha256":"57d66c3a747492fa0ba904c572c8acb8477369fd49a7d9833e29d7d038721c78",
        "passing":["named_amd","named_arm","custom","darwin"]}
    rows=selector("native", RACE)
    r["selection_checks"]["native"]=all(l+"_musl" in dict(rows) and sum(k==l+"_impl" and "linux_x86_64_musl" in v for k,v in rows)==1 for l in ROOTS)
    r["normal_ready"]=r["selection_checks"]["native"];save()
    rootmap=json.loads(command("root_mapping",["bazel","mod","dump_repo_mapping",""],120).read_text())
    repo=None
    for i,root in enumerate((rootmap.get("rules_cc"),rootmap.get("bazel_tools","bazel_tools"))):
        if not root:continue
        mapping=json.loads(command("mapping"+str(i),["bazel","mod","dump_repo_mapping",root],120).read_text())
        repo=mapping.get("local_config_cc_toolchains") or mapping.get("local_config_cc_toolchain")
        if repo:
            r["gnu_implementation_repo"]=mapping.get("local_config_cc");break
    repo=repo or rootmap.get("local_config_cc_toolchains")
    assert repo and re.fullmatch(r"[a-zA-Z0-9._+~-]+",repo)
    r["gnu_toolchains"]="@@"+repo+"//:all";save()
    rows=selector("explicit_gnu",[*RACE,"--extra_toolchains="+r["gnu_toolchains"]])
    for l in ROOTS: assert (l+"_musl") not in dict(rows) and (l+"_impl") in dict(rows)
    gcc3=command("host_static_lib",["gcc","-print-file-name=libc.a"],30).read_text().strip()
    p=Path(gcc3);assert p.is_absolute() and p.is_file() and p.stat().st_size>100000
    r["host_libc_a"]=sig(p);save();assert r["normal_ready"]

def events(file):
    with file.open() as rows:
        for line in rows:
            if line.strip():yield json.loads(line)

def build():
    bep=DIR/"build.bep";call=command("build",["bazel","build",*RACE,"--keep_going","--build_event_publish_all_actions","--build_event_json_file="+str(bep),*ROOTS],1800,False)
    rows=list(events(bep));r["completed"]={e["id"]["targetCompleted"]["label"]:e.get("completed",{}).get("success",False) for e in rows if e["id"].get("targetCompleted",{}).get("label") in ROOTS}
    r["links"]={e["id"]["actionCompleted"].get("label",""):e["id"]["actionCompleted"]["primaryOutput"] for e in rows if e.get("action",{}).get("type")=="GoLink" and e["action"].get("success") and e["id"].get("actionCompleted",{}).get("label") in {x+"_impl" for x in ROOTS}}
    save();assert r["commands"]["build"]["exit"]==0 and all(r["completed"].get(l) for l in ROOTS) and len(r["links"])==2
    for i,l in enumerate(ROOTS):
        files=evaluate('str(target.files.to_list()[0].path) if len(target.files.to_list())==1 else "NOT_SINGLETON"',"public"+str(i),RACE,l).strip()
        p=Path(files);assert files.startswith("bazel-out/") and files.endswith("/bin/"+norm(l)) and p.is_symlink() and p.samefile(r["links"][l+"_impl"])
        elf=command("elf"+str(i),["readelf","-hlW",str(p)],35).read_text();raw=p.read_bytes()
        r.setdefault("binaries",{})[l]=dict(**sig(p),public_file=files,source=r["links"][l+"_impl"],static=" INTERP " not in elf and " DYNAMIC " not in elf,machine=int.from_bytes(raw[18:20],"little"),race=b"runtime.raceinit" in raw and b"__tsan_" in raw)
        save();assert r["binaries"][l]["static"] and r["binaries"][l]["machine"]==62 and r["binaries"][l]["race"]
        if not i:
            txt=command("cli",[str(p),"--version"],20).read_text();assert SOURCE[:9] in txt;r["cli_stamp"]=SOURCE[:9]
    query='mnemonic("CopyFile", //:release)'
    output=command("staging",["bazel","aquery",*RACE,"--output=text","--include_commandline=false",query],300).read_text()
    r["staging"]={l:any(r["binaries"][l]["public_file"] in block and output_name in block for block in output.split("\naction ") ) for l,output_name in zip(ROOTS,("release/runsc","release/gvisor-bin/gvisor_sentry"))}
    save();assert all(r["staging"].values())

def test():
    bep=DIR/"test.bep";tests=(*NOGO,*POLICY)
    command("test",["bazel","test",*BASE,"--nocache_test_results","--keep_going","--build_event_publish_all_actions","--build_event_json_file="+str(bep),*tests],3000,False)
    rows=events(bep);r["tests"]={};r["analysis_actions"]=[]
    for e in rows:
        action=e.get("action",{});aid=e["id"].get("actionCompleted",{});label=aid.get("label","")
        if action.get("type")=="GoStaticAnalysis" and label in {l+"_impl" for l in ROOTS}:
            r["analysis_actions"].append({"label":label,"primary":aid["primaryOutput"],"cfg":aid.get("configuration",{}).get("id"),"success":bool(action.get("success"))})
        if "testResult" not in e["id"] or e["id"]["testResult"]["label"] not in tests:continue
        k=e["id"]["testResult"];v=e["testResult"];l=k["label"]
        o={a["name"]:a for a in v.get("testActionOutput",[]) if a["name"] in ("test.xml","test.log")}
        result={"status":v["status"],"fresh":not v.get("cachedLocally") and not v.get("executionInfo",{}).get("cachedRemotely"),"run":k["run"],"shard":k["shard"],"attempt":k["attempt"]}
        for f,a in o.items():
            uri=urlparse(a["uri"]);assert uri.scheme=="file" and uri.netloc in ("","localhost")
            p=Path(unquote(uri.path));assert p.name==f and norm(l) in p.as_posix();result[f]=sig(p)
            if f=="test.xml":
                cases=list(ElementTree.parse(p).iter("testcase"));result["cases"]=len(cases);result["skipped_or_failed"]=sum(bool(set(x.tag for x in c)&{"skipped","error","failure"}) for c in cases)
        r["tests"][l]=result
    save()
    aquery='mnemonic("GoStaticAnalysis", deps(set(%s)))'%" ".join(NOGO)
    out=command("analyzer",["bazel","aquery",*BASE,"--include_aspects","--output=jsonproto","--noinclude_artifacts",aquery],420)
    data=json.loads(out.read_bytes());targets={str(t["id"]):local(t["label"]) for t in data["targets"]};configs={str(t["id"]):t.get("cpu") for t in data["configuration"]}
    r["analyzer_inputs"]=[];filters={}
    for a in data.get("actions",[]):
        label=targets.get(str(a["targetId"]));args=a.get("arguments",[]);cpu=configs.get(str(a.get("configurationId")))
        if label in NOGO and "filter" in args:
            f,=[s.removeprefix("-output=") for s in args if s.startswith("-output=")]
            if f.startswith("bazel-out/") and Path(f).is_file():
                filters[f]=set(v for v in args if v.endswith("_impl.findings"));r.setdefault("filters",{})[f]=sig(Path(f))
        if label in {l+"_impl" for l in ROOTS} and "check" in args:
            expected="runsc/main.go" if label.startswith("//runsc:") else "runsc/cmd/sentry/sentry_main.go"
            f,=[v.removeprefix("-findings=") for v in args if v.startswith("-findings=")]
            assert f.startswith("bazel-out/") and f.endswith("_impl.findings") and expected in args
            row={"label":label,"cpu":cpu,"source":expected,"output":f}
            if Path(f).is_file():row["bytes"]=sig(Path(f))
            r["analyzer_inputs"].append(row)
    save()
    assert r["commands"]["test"]["exit"]==0 and set(r["tests"])==set(tests)
    assert all(v["status"]=="PASSED" and v["fresh"] and v["run"]==v["shard"]==v["attempt"]==1 and v.get("cases",0)>0 and not v["skipped_or_failed"] for v in r["tests"].values())
    for l in ROOTS:
        for cpu in ("aarch64","k8"):
            x,=[x for x in r["analyzer_inputs"] if x["label"]==l+"_impl" and x["cpu"]==cpu and "bytes" in x]
            assert any(a["label"]==x["label"] and a["success"] and a["primary"]==x["output"] for a in r["analysis_actions"])
            suffix=norm(l)+"_nogo."+{"k8":"amd64","aarch64":"arm64"}[cpu]+".findings"
            assert any(f.endswith("/"+suffix) and x["output"] in seen for f,seen in filters.items())

def gnu():
    selection=json.loads((PUBLIC/"selection.json").read_text())
    flags=[*RACE,"--extra_toolchains="+selection["gnu_toolchains"]]
    label=ROOTS[0];expr='mnemonic("GoLink", deps('+label+',2))'
    out=command("gnu_aquery",["bazel","aquery",*flags,"--include_param_files","--noinclude_artifacts","--output=jsonproto",expr],300)
    data=json.loads(out.read_bytes());targets={str(t["id"]):local(t["label"]) for t in data["targets"]}
    a,=[a for a in data.get("actions",[]) if targets.get(str(a["targetId"]))==label+"_impl"]
    argv=a.get("arguments",[])+[v for pf in a.get("paramFiles",[]) for v in pf.get("arguments",[])]
    cc="@bazel_tools//tools/cpp:current_cc_toolchain"
    owners=evaluate("str(target.label)","gnu_toolchain_owner",flags,'kind("^cc_toolchain rule$", deps('+cc+', 1))').splitlines()
    implementation=selection["gnu_implementation_repo"];assert re.fullmatch(r"[a-zA-Z0-9._+~-]+",implementation)
    selected=[l for l in owners if l.startswith("@@"+implementation+"//") or l.startswith("@"+implementation+"//")]
    driver=evaluate('str(providers(target)["ToolchainInfo"].cc.compiler_executable)',"gnu_driver",flags,cc).strip()
    # Join the actual Go external linker argument to the selected C toolchain.
    used=driver in argv or any(v=="-extld="+driver or v=="-extld "+driver for v in argv)
    real=Path(driver).resolve() if Path(driver).is_absolute() else None
    gcc=bool(real and re.fullmatch(r"(?:[A-Za-z0-9_.+-]*-)?(?:gcc|g\\+\\+)(?:-[0-9]+)?",real.name))
    r["gnu_link"]={"owner":label+"_impl","selected":selected,"compiler":real.name if gcc else None,"same_driver":used,"static":any("-static" in v for v in argv),"capability":selection.get("host_libc_a")};save()
    if len(selected)!=1 or not gcc or not used or not r["gnu_link"]["static"]:raise ValueError("GNU owner or driver mismatch")
    command("gnu_build",["bazel","build",*flags,label],1320)
    p=Path(evaluate('str(target.files.to_list()[0].path)',"gnu_output",flags,label).strip())
    assert str(p).endswith("/bin/"+norm(label)) and p.is_symlink()
    e=command("gnu_elf",["readelf","-hlW",str(p)],35).read_text();b=p.read_bytes()
    r["binary"]=dict(**sig(p),machine=int.from_bytes(b[18:20],"little"),static=" INTERP " not in e and " DYNAMIC " not in e,race=b"runtime.raceinit" in b and b"__tsan_" in b);save()
    assert r["binary"]["machine"]==62 and r["binary"]["static"] and r["binary"]["race"]
    assert SOURCE[:9] in command("gnu_version",[str(p),"--version"],20).read_text()

mode=sys.argv[1]
r={"source":SOURCE,"carrier":os.environ["GITHUB_SHA"],"status":"INCOMPLETE","commands":{}}
try:
    {"selection":check_selection,"link":build,"policy":test,"gnu":gnu}[mode]()
    r["status"]="PASS"
except (OSError,ValueError,KeyError,TypeError,IndexError,AssertionError) as e:
    r["error"]=type(e).__name__;r["line"]=e.__traceback__.tb_next.tb_lineno if e.__traceback__.tb_next else e.__traceback__.tb_lineno
save()
raise SystemExit(r["status"]!="PASS")
