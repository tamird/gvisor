"""Run the same freestanding target with Coral and LLVM on a compiler-free worker."""
import json
import os
import re
import struct
import subprocess
from pathlib import Path

root=Path(os.environ["RELEASE_TEMP"])
out=root/"results"
target="//runsc/prewarmer:prewarmer_binary"
report={"expected":["worker","stock-arm64","llvm-amd64","llvm-arm64"],"cases":{}}
def records(path):
    data=path.read_text()
    dec=json.JSONDecoder()
    pos=0
    while pos<len(data):
        if data[pos].isspace():
            pos+=1
            continue
        value,pos=dec.raw_decode(data,pos)
        if isinstance(value,list):
            yield from value
        else:
            yield value
def run(name, folder, args, expect):
    log=root/(name+".exec.json")
    cmd=["bazelisk","--bazelrc="+str(root/"private.bazelrc"),"build","-c","opt",
         "--noremote_accept_cached","--remote_download_outputs=toplevel",
         "--execution_log_json_file="+str(log),*args]
    logfile=root/(name+".log")
    with logfile.open("wb") as f:
        result=subprocess.run(cmd,cwd=folder,stdout=f,stderr=subprocess.STDOUT)
    row=report["cases"][name]={"exit":result.returncode}
    text=logfile.read_text(errors="replace")
    if result.returncode:
        # Store only diagnostic text; raw logs stay on the runner.
        errors=[line[:800] for line in text.splitlines() if not re.search(r'api.key|header|credential|token|bearer',line,re.I) and ("No such file" in line or "not found" in line or line.startswith(("ERROR:", "Error ", "Error:")))]
        row["diagnostics"]=errors[-4:]
    spawns=[]
    if log.exists():
        for obj in records(log):
            argv="\n".join(obj.get("commandArgs",[]))
            if "prewarmer.c" not in argv and "tool-absent.txt" not in argv:
                continue
            if obj.get("mnemonic")!="Genrule":
                continue
            props={e["name"]:e["value"] for e in obj.get("platform",{}).get("properties",[])}
            spawns.append({"runner":obj.get("runner"),"cache_hit":obj.get("cacheHit",False),"image":props.get("container-image"),
                           "clang":re.findall(r'[\w./+~-]+/clang(?:\+\+)?(?:-[0-9]+)?(?=\s|$)',argv),
                           "lld":re.findall(r'[\w./+~-]+/ld\.lld(?:-[0-9]+)?(?=\s|$)',argv),
                           "target":re.findall(r'(?:--?target(?:=|\s+))([\w.-]+)',argv)})
    row["spawns"]=spawns
    remote=any(s["runner"]=="remote" and not s["cache_hit"] and s["image"]=="docker://"+os.environ["IMAGE"] for s in spawns)
    if name=="worker":
        row["pass"]=result.returncode==0 and remote
    elif expect is None:
        row["pass"]=result.returncode!=0 and remote and bool(re.search(r'(?:aarch64-linux-gnu-gcc|/usr/bin/gcc)[^\n]*(?:No such file|not found)',text))
    else:
        if not result.returncode:
            binary=(folder/"bazel-bin/runsc/prewarmer/gvisor-sentry-prewarmer").read_bytes()
            row["elf_machine"]=struct.unpack_from("<H",binary,18)[0] if binary.startswith(b"\x7fELF") else None
        row["pass"]=result.returncode==0 and row.get("elf_machine")==expect and any(s["runner"]=="remote" and not s["cache_hit"] and s["image"]=="docker://"+os.environ["IMAGE"] and s["clang"] and s["lld"] for s in spawns)
    (out/"compiler-cases.json").write_text(json.dumps(report,indent=2)+"\n")
    print(name,row,flush=True)
run("worker",root/"source",["@release_platform//:without_compiler"],True)
run("stock-arm64",root/"stock",["--config=aarch64",target],None)
for n,cfg,expected in (("amd64","x86_64",62),("arm64","aarch64",183)):
    run("llvm-"+n,root/"source",["--config="+cfg,target],expected)
