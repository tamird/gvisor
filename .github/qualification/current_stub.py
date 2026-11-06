"""Check the reviewed stub and sidecar ELF outputs with the released compiler."""
import hashlib, json, os, re, struct, subprocess
from pathlib import Path
import xml.etree.ElementTree as ET

SOURCE="0e95a4eb4df06d1d02d47f9a268f35597e5b3913"
ROOT=Path(os.environ["RUNNER_TEMP"])/"current-stub"
PKG="//pkg/sentry/platform/systrap/sysmsg:"
STUB,GO,BIN=[PKG+n for n in ("sighandler_object","sighandler_go","sighandler_binary")]
SIDE=["//runsc/prewarmer:prewarmer_binary","//runsc/procumounter:procumounter_binary","//runsc/fdparking:fdparking_binary"]
FLAGS=["-c","opt","--jobs=4","--remote_executor=","--remote_cache=","--bes_backend=","--disk_cache=","--noshow_progress","--color=no","--curses=no"]
FORMAT='str(target.label) + " " + ",".join([f.path for f in target.files.to_list()])'
report={"source":SOURCE,"cases":{}}

def save():
    (ROOT/"receipt.json").write_text(json.dumps(report,indent=2)+"\n")

def run(row,command,flags,*args):
    p=subprocess.run(["bazelisk","--nosystem_rc","--nohome_rc",command,*FLAGS,*flags,*args],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    row[command+"_exit"]=p.returncode
    if p.returncode:
        row[command+"_errors"]=[l[:420] for l in p.stdout.splitlines() if re.search(r"^ERROR:|error:|Error in|no such target|undefined reference|linker command failed",l,re.I)][:18]
    save()
    return p

def elf(path):
    data=path.read_bytes()
    assert data[:6]==b"\x7fELF\x02\x01"
    kind,machine=struct.unpack_from("<HH",data,16)
    headers=subprocess.check_output(["readelf","-lW",str(path)],text=True)
    return {"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest(),"machine":machine,"type":kind,"interp":any(l.split()[:1]==["INTERP"] for l in headers.splitlines())}

def measure(name,flags,machine,pressure=False):
    row=report["cases"][name]={}
    labels=[STUB,*SIDE] if pressure else [STUB,GO,BIN,"//vdso:vdso",*SIDE]
    build=run(row,"build",flags,"--keep_going",*labels)
    out=run(row,"cquery",flags,"--output=starlark","--starlark:expr="+FORMAT,"set(%s)"%" ".join(labels))
    if out.returncode:
        return False
    paths={}
    for line in out.stdout.splitlines():
        key,sep,value=line.partition(" ")
        label=key.lstrip("@")
        if sep and label in labels and Path(value).is_file():
            paths[label]=Path(value)
    row["missing_outputs"]=sorted(set(labels)-paths.keys())
    row["elfs"]={k:elf(v) for k,v in paths.items() if k not in (GO,BIN)}
    save()
    if STUB not in paths:
        return False
    table=subprocess.check_output(["readelf","-Ws",str(paths[STUB])],text=True)
    symbols={}
    for line in table.splitlines():
        fields=line.split()
        if len(fields)>=8 and fields[6]!="UND" and fields[7].startswith("__export_"):
            symbols[fields[7].replace(".","_")]=int(fields[1],16)
    sections=subprocess.check_output(["readelf","-WS",str(paths[STUB])],text=True)
    row.update(exports=symbols,crblob=".crblob" in sections,lbss=".lbss" in sections)
    if not pressure and GO in paths and BIN in paths:
        raw=paths[BIN].read_bytes()
        gos={k:int(v,16) for k,v in re.findall(r"^var Sighandler_blob_offset__(\w+) = 0x([0-9a-f]+)$",paths[GO].read_text(),re.M)}
        row.update(go_offsets=gos,flat_bytes=len(raw),flat_sha256=hashlib.sha256(raw).hexdigest())
    save()
    valid=build.returncode==0 and not row["missing_outputs"] and all(v["machine"]==machine and not v["interp"] for v in row["elfs"].values())
    valid=valid and all(row["elfs"][k]["bytes"]<=4096 for k in SIDE[:2])
    valid=valid and len(symbols)==(10 if machine==62 else 11) and row["crblob"] and not row["lbss"]
    return valid and (pressure or row["go_offsets"]==symbols and all(0<=v<row["flat_bytes"] for v in symbols.values()))

assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==SOURCE
save()
passed=[]
for arch,machine in [("x86_64",62),("aarch64",183)]:
    passed.append(measure(arch,["--config="+arch],machine))
passed.append(measure("amd64_override",["--config=x86_64","--features=mcmodel_medium","--custom_malloc=//test/util:errno_safe_allocator","--linkopt=-fsanitize=address"],62,True))
row=report["prewarmer"]={}
result=run(row,"test",["--config=x86_64"],"--nocache_test_results","--strategy=TestRunner=local","--test_arg=-test.v","--test_env=GO_TEST_WRAP_TESTV=1","//runsc/prewarmer:prewarmer_single_page_test")
if not result.returncode:
    cases=ET.parse("bazel-testlogs/runsc/prewarmer/prewarmer_single_page_test/test.xml").findall(".//testcase")
    row["passed"]=[c.attrib["name"] for c in cases if all(c.find(tag) is None for tag in ("failure","error","skipped"))]
save()
assert all(passed) and result.returncode==0 and "TestPrewarmerFitsInOnePage" in row["passed"]
report["complete"]=True
save()
