"""Build the two unchanged public race programs on the supplier's static GNU platform."""
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile

SOURCE, RECEIPT = sys.argv[1:]
TARGETS = ("//runsc:runsc-race", "//runsc/cmd/sentry:gvisor_sentry-race")
PLATFORM = "@llvm//platforms:linux_x86_64_glibc_static"
FLAGS = ["--config=x86_64", "-c", "opt", "--config=race", "--platforms=" + PLATFORM,
         "--jobs=4", "--remote_cache=", "--remote_executor=", "--disk_cache="]
SOURCES = {"runsc/BUILD":"27e9a762f62da298b6cfe5c6b52e76da0dbfa134", "runsc/cmd/sentry/BUILD":"b5bef6d13d48ab79f5d1f83f439d90a2c5b2e52a",
           "tools/bazeldefs/go.bzl":"47eab30168e4c2a9c9e526c88abc056f86525de6", "tools/defs.bzl":"5b1734ef57f7b90e8f42c86349fdd1d1cf0b6597"}
SUPPLIER = "bf95c202b354da28a6114282e7a78006dc612c58afb2eb1de407ac7082803050"
r = {"source":SOURCE, "status":"INCOMPLETE", "platform":PLATFORM,"commands":{}}

def save():
    Path(RECEIPT).write_text(json.dumps(r, indent=2) + "\n")

def require(ok, what):
    if not ok:
        raise ValueError(what)

def run(name, argv, cap, check=True):
    r["phase"] = name
    save()
    with (task/(name+".out")).open("wb") as out, (task/(name+".err")).open("wb") as err:
        p = subprocess.run(["timeout","-k","10",str(cap),*map(str,argv)],stdout=out,stderr=err)
    e=(task/(name+".err")).read_bytes()
    diag = {}
    for key,regex in (("missing_library",rb"unable to find library (-l[A-Za-z0-9_.+-]+)"),
                      ("undefined_symbol",rb"(?:undefined symbol: |undefined reference to [\x60'])([A-Za-z_.$][A-Za-z0-9_.$@]{0,120})"),
                      ("duplicate_symbol",rb"duplicate symbol: ([A-Za-z_.$][A-Za-z0-9_.$@]{0,120})"),
                      ("other",rb"(No space left on device|out of memory|No such file or directory|not compatible|Analysis of target|Permission denied|Segmentation fault)")):
        hits=sorted({m.decode() for m in re.findall(regex,e)})
        if hits: diag[key]=hits[:24]
    r["commands"][name]={"exit":p.returncode,"stderr_sha256":hashlib.sha256(e).hexdigest(),"diagnostics":diag}
    save()
    if check: require(not p.returncode, name)
    return (task/(name+".out")).read_bytes()

def elf(p):
    raw=p.read_bytes()
    require(raw[:6]==b"\x7fELF\x02\x01","ELF64 little endian")
    et, machine=struct.unpack_from("<HH",raw,16)
    phoff,shoff=struct.unpack_from("<QQ",raw,32)
    phsize,phcount,shsize,shcount,names=struct.unpack_from("<5H",raw,54)
    ptypes=[struct.unpack_from("<I",raw,phoff+i*phsize)[0] for i in range(phcount)]
    sh=[struct.unpack_from("<IIQQQQIIQQ",raw,shoff+i*shsize) for i in range(shcount)]
    strings=raw[sh[names][4]:sh[names][4]+sh[names][5]]
    needed=False
    live=set()
    for s in sh:
        name=strings[s[0]:].split(b"\0",1)[0]
        b=raw[s[4]:s[4]+s[5]]
        if s[1]==6:  # SHT_DYNAMIC; static PIE itself may have PT_DYNAMIC.
            needed |= any(struct.unpack_from("<q",b,n)[0]==1 for n in range(0,len(b),16))
        if name not in (b".gopclntab",b".data.rel.ro.gopclntab",b".rodata",b".data.rel.ro"):continue
        marker=b"\xf1\xff\xff\xff\0\0\x01\x08"
        pos=b.find(marker)
        while pos>=0:
            if pos+72<=len(b):
                n,_,_,fname,_,_,_,ftab=struct.unpack_from("<8Q",b,pos+8)
                if 0<n<2000000 and 0<fname<ftab and pos+ftab+n*8<=len(b):
                    for i in range(n):
                        off=struct.unpack_from("<I",b,pos+ftab+i*8+4)[0]
                        entry=pos+ftab+off
                        if entry+8>len(b):continue
                        idx=struct.unpack_from("<i",b,entry+4)[0]
                        if idx<0:continue
                        start=pos+fname+idx
                        for sym in (b"runtime.raceinit",b"runtime.racefini"):
                            if b[start:start+len(sym)+1]==sym+b"\0":live.add(sym.decode())
            pos=b.find(marker,pos+1)
    return {"machine":machine,"elf_type":et,"static":3 not in ptypes and not needed,"race_functions":sorted(live),
            "bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}

def main():
    require(re.fullmatch(r"[0-9a-f]{40}",SOURCE) and os.uname().machine=="x86_64","source or host")
    require(run("revision",["git","rev-parse","HEAD"],15).decode().strip()==SOURCE,"source checkout")
    require(run("bazel_version",["bazel","--version"],90).strip()==b"bazel 9.3.1","Bazel version")
    records=run("trees",["git","ls-tree",SOURCE,*SOURCES],15).decode().splitlines()
    require({p:v.split()[-1] for v,p in (l.split("\t") for l in records)}==SOURCES,"unchanged public race declarations/adapters")
    mod=Path("MODULE.bazel").read_text()
    require(SUPPLIER in mod and re.search(r"hermetic-llvm/archive/29f9d4664[0-9a-f]{31}[.]tar[.]gz",mod),"resolver supplier pin")
    r["supplier_archive_sha256"]=SUPPLIER
    roots="set("+" ".join(TARGETS)+")"
    # Run the product even if the later evidence projection is insufficient.
    bep=task/"race.bep"
    run("build",["bazel","build",*FLAGS,"--keep_going","--build_event_publish_all_actions","--build_event_json_file="+str(bep),*TARGETS],2700,False)
    actions={};complete={};r["test_results"]=0
    if bep.is_file():
        for line in bep.open():
            e=json.loads(line); k=e.get("id",{}); a=k.get("actionCompleted",{});t=k.get("targetCompleted",{});v=e.get("action",{})
            if a.get("label") in TARGETS and v.get("type")=="GoLink":
                actions[a["label"]]={"cfg":a.get("configuration",{}).get("id"),"output":a.get("primaryOutput"),"success":v.get("success",False),"exit":v.get("exitCode",0)}
            if t.get("label") in TARGETS and not t.get("aspect"):complete[t["label"]]=e.get("completed",{}).get("success",False)
            r["test_results"]+=int("testResult" in k)
    r.update(actions=actions,completed=complete);save()
    expr='str(target.label)+"\\t"+str(build_options(target)["//command_line_option:platforms"])+"\\t"+(target.files.to_list()[0].path if len(target.files.to_list())==1 else "-")'
    outputs={}
    for row in run("configured",["bazel","cquery",*FLAGS,"--output=starlark","--starlark:expr="+expr,roots],360).decode().splitlines():
        l,plat,f=row.split("\t");l="//"+l.split("//",1)[-1]
        require(l in TARGETS and "linux_x86_64_glibc_static" in plat,"public configured target")
        outputs[l]=f
    r["configured_outputs"]=outputs;save()
    data=json.loads(run("links",["bazel","aquery",*FLAGS,"--noinclude_artifacts","--include_param_files","--output=jsonproto",'mnemonic("GoLink",'+roots+')'],360))
    ts={str(x["id"]):"//"+x["label"].split("//",1)[-1] for x in data.get("targets",[])}
    cfg={str(c["id"]):c["checksum"] for c in data.get("configuration",[])}
    links={}
    for a in data.get("actions",[]):
        l=ts.get(str(a.get("targetId")))
        if l not in TARGETS or a.get("mnemonic")!="GoLink":continue
        args=a.get("arguments",[])+[v for f in a.get("paramFiles",[]) for v in f.get("arguments",[])]
        links[l]={"cfg":cfg[str(a["configurationId"])],"action_key":a["actionKey"],"static_external":any("-static" in s.split() for s in args),
                  "glibc_search":any("glibc_library_search_directory" in s for s in args),"race_link":any(s=="-race" or "-race" in s.split() for s in args)}
    r["configured_links"]=links;save()
    if r["commands"]["build"]["exit"]:r["status"]="BUILD_FAILED";save();return
    require(set(actions)==set(complete)==set(outputs)==set(links)==set(TARGETS) and not r["test_results"],"two public links")
    r["binaries"]={}
    for i,l in enumerate(TARGETS):
        a=actions[l];q=links[l];f=Path(outputs[l])
        require(complete[l] and a["success"] and a["exit"]==0 and a["cfg"]==q["cfg"] and q["static_external"] and q["glibc_search"],"link/config join")
        require(not f.is_absolute() and f.parts[0]=="bazel-out" and ".." not in f.parts and f.samefile(a["output"]),"file/GoLink join")
        e=elf(f);r["binaries"][l]=e;save()
        require(e["static"] and e["machine"]==62 and e["race_functions"]==["runtime.racefini","runtime.raceinit"],"static AMD64 race executable")
        if i==0:
            v=run("runsc_version",[f,"--version"],30)
            require(SOURCE[:9].encode() in v,"native source stamp");r["native_version_stamp"]=SOURCE[:9];save()
    r["status"]="PASS";save()

with tempfile.TemporaryDirectory(prefix="static-gnu-race-",dir=os.environ["RUNNER_TEMP"]) as d:
    task=Path(d)
    try:main()
    except (OSError,ValueError,KeyError,TypeError,IndexError,struct.error) as ex:
        r["error"]={"class":type(ex).__name__,"reason":str(ex)[:170] if isinstance(ex,ValueError) else None}
        save()
sys.exit(r["status"]!="PASS")
