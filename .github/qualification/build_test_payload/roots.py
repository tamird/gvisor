"""Record build-only membership and normalized configured output sets on the original mixed source."""
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from check import BASE, CLIENT, CONFIG, OUTPUT, WORK, source

ROOTS=set((WORK/"original.txt").read_text().splitlines())
RUN=json.loads((WORK/"runnable.json").read_text())
assert len(ROOTS)==2880
FORM='''def format(target):
    name=str(target.label).lstrip("@")
    if name not in originals:
        return ""
    op=build_options(target) or {}
    if op.get("//command_line_option:is exec configuration",False):
        return ""
    platforms=str(op.get("//command_line_option:platforms",""))
    info=(providers(target) or {})
    default=info.get("DefaultInfo")
    groups=info.get("OutputGroupInfo")
    files=target.files.to_list()
    rf=default.default_runfiles if default else None
    syms=len(rf.symlinks.to_list())+len(rf.root_symlinks.to_list()) if rf else 0
    runs=rf.files.to_list() if rf else []
    findings=getattr(groups,"nogo_findings",depset()).to_list() if groups else []
    return "\\t".join([name, platforms,str(op.get("//command_line_option:cpu","")), str(syms),
        "|".join([("S" if f.is_source else "D")+f.path for f in files]),
        "|".join([("S" if f.is_source else "D")+f.short_path for f in runs]),
        "|".join([f.short_path for f in findings])])
'''


def targetset(labels):
    return "set("+" ".join(sorted(labels))+")"


def digest(items):
    return hashlib.sha256(("\n".join(sorted(set(items)))+"\n").encode()).hexdigest()


def dump(name,obj):
    (OUTPUT/(name+".json")).write_text(json.dumps(obj,sort_keys=True,separators=(",",":"))+"\n")


def observe(tag,mode,expression,star,top):
    cfg=WORK/(tag+".query")
    cfg.write_text(expression+"\n")
    out=WORK/(tag+".cquery")
    with out.open("wb") as fd,(WORK/(tag+".log")).open("wb") as err:
        c=subprocess.run([*CLIENT,"cquery","--config="+CONFIG[mode],"--jobs=4","--remote_cache=",
            "--bes_backend=","--remote_executor=","--spawn_strategy=local","--noshow_progress",
            "--output=starlark","--starlark:file="+str(star),"--query_file="+str(cfg)],stdout=fd,stderr=err)
    data=defaultdict(list)
    with out.open() as fd:
        for line in fd:
            if not line.strip():continue
            label,platform,cpu,syms,default,runs,findings=line.rstrip("\n").split("\t")
            assert label in ROOTS
            arch=next((a for a in ("amd64","arm64") if "linux_"+a in platform),None)
            arch=arch or {"k8":"amd64","aarch64":"arm64"}.get(cpu,"other")
            files=[s for s in default.split("|") if s]
            normalized=[re.sub(r"^Dbazel-out/[^/]+/(?:genfiles|bin)/","D",f) for f in files]
            runfiles=[s for s in runs.split("|") if s]
            kinds=[a for a in ("amd64","arm64") if any(f.endswith("."+a+".findings") for f in findings.split("|")) and any(f.endswith("."+a+".findings") for f in runfiles)]
            fixed=[p for p in files if p.startswith("D")]
            short=[f[1:] for f in normalized if f[0]=="D"]
            value={"arch":arch,"n":len(files),"runfiles":len(runfiles),"symlinks":int(syms),
                   "defaults":digest(normalized),"runfile_names":digest(runfiles),"nogo":kinds,
                   "only_source":bool(files+runfiles) and all(s[0]=="S" for s in files+runfiles) and int(syms)==0,
                   "empty":not files and not runfiles and int(syms)==0,
                   "native_shaped":any(s.endswith((".a",".x",".so",".pic.o",".o")) or "/"+s.split("/")[-1]+"_/" in s for s in short),
                   "generated_defaults":len(fixed)}
            if value not in data[label]:data[label].append(value)
    dump(tag,{"exit":c.returncode,"source":BASE,"mode":mode,"top_level_only":top,
              "owner_count":len(data),"missing":sorted(ROOTS-data.keys()),"products":data})
    return data


def main():
    assert source()==BASE
    star=WORK/"build-roots.star"
    star.write_text("originals = "+repr({s:1 for s in sorted(ROOTS)})+"\n"+FORM)
    expr=targetset(ROOTS)
    modes={}
    for mode in ("amd64","arm64","mixed"):
        modes[mode]=observe("roots-"+mode,mode,"config("+expr+",target)",star,True)
    closure="deps("+targetset(ROOTS|set(RUN["ordinary"])|set(RUN["mixed_extra"]))+")"
    mixed=observe("roots-mixed-resolved","mixed",closure,star,False)
    # Rule kinds are a read-only loading query; distinguish native archives, generators,
    # and filegroups when deciding whether identical short names are meaningful.
    query=WORK/"original-kinds.q";query.write_text(expr)
    kindrun=subprocess.run([*CLIENT,"query","--output=label_kind","--query_file="+str(query)],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
    kinds={}
    for line in kindrun.stdout.splitlines():
        if " rule " in line:
            kind,label=line.split(" rule ",1)
            if label in ROOTS:kinds[label]=kind
    rows=[]
    for label in sorted(ROOTS):
        original=[modes[a].get(label,[]) for a in ("amd64","arm64")]
        arm=[r for r in mixed.get(label,[]) if r["arch"]=="arm64"]
        row={"label":label,"kind":kinds.get(label,"unknown"),"mixed_arm_config":bool(arm)}
        if all(len(x)==1 for x in original):
            a,b=[x[0] for x in original]
            same=a["defaults"]==b["defaults"] and a["runfile_names"]==b["runfile_names"] and a["symlinks"]==b["symlinks"]==0
            matched=any(x["defaults"]==b["defaults"] and x["runfile_names"]==b["runfile_names"] and not(x["symlinks"] or b["symlinks"]) for x in arm)
            row["arm_names_match"]=matched
            if a["nogo"]==b["nogo"]==["amd64","arm64"]:category="already-split-nogo"
            elif a["empty"] and b["empty"]: category="empty"
            elif a["only_source"] and b["only_source"] and same: category="same-source-files"
            elif a["native_shaped"] or b["native_shaped"]: category="native-output-names"
            elif not same: category="different-output-or-runfile-layout"
            else:category="same-names-generated-or-runfiles"
        else:category="unresolved-top-configuration"
        row["category"]=category;rows.append(row)
    dump("build-roots",{"source":BASE,"kind_count":len(kinds),"classes":dict(Counter(r["category"] for r in rows)),"owners":rows,
        "limit":"Configured files and runfile names. Matching names cannot establish equal producer commands or bytes."})


if __name__=="__main__":
    main()
