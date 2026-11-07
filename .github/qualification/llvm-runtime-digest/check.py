"""Measure a single LLVM cc_library with Bazel's ephemeral disk action cache."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SOURCE = "35aa6228edcf1eb2bc07d6aa877b4317da27d68a"
OWNER = "@llvm//runtimes:empty"
WORK = Path(os.environ["RUNNER_TEMP"]) / "llvm-runtime-digest"
CLI = [str(WORK/"bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx6g"]
PLATFORMS = {"linux": "//tools/bazeldefs:linux_amd64", "ubuntu": "//test/rbe:ubuntu_amd64"}
SUPPLIER = "--modify_execution_info=CppModuleMap=+supports-path-mapping,CppArchive=+supports-path-mapping,CppCompile=+supports-path-mapping"
HEX = re.compile(r"[a-f0-9]{64}\Z")
OBJECT = re.compile(r"(?:^|/)_objs/empty/empty(?:\.pic)?\.o\Z")
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip() == SOURCE
mode, spelling = sys.argv[1:]
assert mode in ("stock", "supplier") and spelling in PLATFORMS
prefix = mode+"-"+spelling
cache = WORK/(mode+"-disk")
cache.mkdir(exist_ok=True)
if spelling=="linux": assert not any(cache.iterdir())
receipt = WORK/(prefix+".json")
r={"source":SOURCE,"owner":OWNER,"mode":mode,"target_platform":PLATFORMS[spelling],"verified":False}

def save():
    receipt.write_text(json.dumps(r,sort_keys=True,indent=1)+"\n")

def digest(raw): return hashlib.sha256(raw).hexdigest()

def call(name,cmd,minutes):
    stdout=WORK/(prefix+"-"+name+".out"); log=WORK/(prefix+"-"+name+".log")
    with stdout.open("wb") as out, log.open("wb") as err:
        try: code=subprocess.run(cmd,stdout=out,stderr=err,timeout=minutes*60,check=False).returncode
        except subprocess.TimeoutExpired: code=124
    r[name+"_exit"]=code
    if code:
        lines=[s for s in log.read_text(errors="replace").splitlines() if s.startswith(("ERROR:", "WARNING: Build"))]
        r[name+"_diagnostic"]=[s.replace(os.environ["GITHUB_WORKSPACE"],"$WORKSPACE")[:550] for s in lines[:3]]
    save()
    return code,stdout

def field(d,*names):
    return next((d[n] for n in names if n in d),None)

def varint(buf,i):
    n=0
    for shift in range(0,70,7):
        b=buf[i];i+=1;n|=(b&127)<<shift
        if b<128:return n,i
    raise ValueError("invalid varint")

def fields(buf):
    i=0
    while i<len(buf):
        k,i=varint(buf,i); tag,wire=k>>3,k&7
        if wire==0: value,i=varint(buf,i)
        else:
            if wire==2: size,i=varint(buf,i)
            else: size={1:8,5:4}[wire]
            assert i+size<=len(buf)
            value=buf[i:i+size];i+=size
        yield tag,value

def decode_digest(buf):
    f=dict(fields(buf)); h=f[1].decode("ascii"); assert HEX.fullmatch(h)
    return h,int(f.get(2,0))

def cas(h,size):
    assert HEX.fullmatch(h)
    paths=[cache/"cas"/h[:2]/h,*cache.glob("*/cas/"+h[:2]+"/"+h)]
    for p in paths:
        if p.is_file():
            raw=p.read_bytes()
            assert digest(raw)==h and (size is None or len(raw)==size)
            return raw
    raise ValueError("missing content-addressed blob")

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

def summarize_compiler(root, configured, observed):
    for name in dict.fromkeys((configured,observed)):
        p=root/name
        if not p.is_file(): continue
        raw=p.read_bytes()
        out=subprocess.run([str(p),"--version"],capture_output=True,text=True,cwd=root,timeout=15,check=False)
        match=re.search(r"\bclang version (\d+(?:\.\d+){1,3})",out.stdout)
        return {"path":name,"sha256":digest(raw),"size":len(raw),"clang_version":match[1] if match else None}
    return None

def main():
    save()
    opts=["--config=x86_64","-c","opt","--platforms="+PLATFORMS[spelling],
          "--host_platform=//tools/bazeldefs:linux_amd64","--extra_execution_platforms=//test/rbe:ubuntu_amd64",
          "--experimental_platform_in_output_dir","--experimental_output_paths=strip","--force_pic",
          "--lockfile_mode=off",
          "--remote_executor=","--remote_cache=","--disk_cache="+str(cache),
          "--remote_upload_local_results","--spawn_strategy=linux-sandbox","--jobs=4"]
    if mode=="supplier": opts.append(SUPPLIER)
    code,path=call("query",CLI+["aquery",*opts,"--output=jsonproto","--include_commandline","--include_artifacts",
                        'mnemonic("^CppCompile$",'+OWNER+')'],8)
    if code:return
    graph=json.loads(path.read_bytes())
    fragments={str(v["id"]):v for v in graph["pathFragments"]}
    def artifact_path(a):
        n=str(a["pathFragmentId"]);parts=[]
        while n in fragments:
            f=fragments[n];parts.append(f["label"]);n=str(f.get("parentId"))
        return "/".join(reversed(parts))
    art={str(a["id"]):artifact_path(a) for a in graph["artifacts"]}
    plan={}
    for a in graph["actions"]:
        p=next((art[str(i)] for i in a.get("outputIds",[]) if OBJECT.search(art[str(i)])),None)
        if not p:continue
        plan[p]={"compiler":a["arguments"][0],"platform":a.get("executionPlatform","").removeprefix("@@"),
                 "opt_in":any(x["key"]=="supports-path-mapping" for x in a.get("executionInfo",[]))}
    r["configured"]=plan
    assert plan and all("/"+PLATFORMS[spelling].rsplit(":",1)[1]+"-" in p for p in plan)
    assert all(a["platform"]=="//test/rbe:ubuntu_amd64" for a in plan.values());save()
    spawnlog=WORK/(prefix+"-spawns.json")
    code,out=call("build",CLI+["build",*opts,"--output_groups=compilation_outputs",
        "--execution_log_json_file="+str(spawnlog),"--noremote_cache_async","--nouse_action_cache",OWNER],16)
    if code:return
    root=Path(subprocess.check_output(CLI+["info","execution_root"],text=True).strip().splitlines()[-1])
    r["spawns"]=[]
    if not spawnlog.is_file():save();return
    for e in json_entries(spawnlog):
        if e.get("mnemonic")!="CppCompile" or not (field(e,"targetLabel","target_label") or "").endswith("//runtimes:empty"):continue
        listed=field(e,"listedOutputs","listed_outputs") or []
        hints=[p for p in listed if OBJECT.search(p)]
        if not hints:continue
        side=hints[0].rsplit("/",1)[-1]
        original=next((p for p in plan if p.rsplit("/",1)[-1]==side),None)
        if not original:continue
        name=field(e,"runner"); row={"runner":name,"cache_hit":bool(field(e,"cacheHit","cache_hit")),
            "configured_output":original,"spawn_output":hints[0],"action_digest":e.get("digest",{}).get("hash")}
        r["spawns"].append(row);save()
        obj=(root/original).read_bytes()
        row["object"]={"sha256":digest(obj),"size":len(obj),"elf":obj[:4]==b"\x7fELF","machine":int.from_bytes(obj[18:20],"little"),"type":int.from_bytes(obj[16:18],"little")}
        ah=row["action_digest"]; action=dict(fields(cas(ah,None)))
        ch,cs=decode_digest(action[1]);root_h,root_size=decode_digest(action[2])
        row.update(command_digest=ch,input_root_digest=root_h,input_root_size=root_size)
        command=list(fields(cas(ch,cs)))
        args=[v.decode() for t,v in command if t==1]
        outs=[v.decode() for t,v in command if t in (3,7) and OBJECT.search(v.decode())]
        row["serialized_outputs"]=outs
        row["argv0"]=args[0]
        row["compiler"]=summarize_compiler(root,plan[original]["compiler"],args[0])
        save()
    r["verified"]=bool(r["spawns"]) and all(row.get("command_digest") and row.get("input_root_digest") and row.get("serialized_outputs") and (row.get("compiler") or {}).get("clang_version") and row["object"]["machine"]==62 and row["object"]["type"]==1 and row["runner"] in ("linux-sandbox","disk cache hit") for row in r["spawns"])
    save()
try: main()
except Exception as e:
    r["diagnostic"]=type(e).__name__+": "+str(e)[:240]
    save()
