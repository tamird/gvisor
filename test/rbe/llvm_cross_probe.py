"""Check gVisor's released LLVM toolchains on one unmodified AMD64 host."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys

SOURCE = "90d4e2253f004cdc73a35dbc4050eab3e9a8495d"
TARGET = "//tools/zstd:compressor"
QEMU = "@@rules_qemu++qemu+qemu_user_prebuilt_linux_amd64_aarch64//:qemu-aarch64"
TASK = Path(os.environ["RUNNER_TEMP"]) / "llvm-cross"
BAZEL = str(TASK / "bazel")
INPUT = (b"gVisor release compressor cross-target control\n" * 16)
sha = lambda data: hashlib.sha256(data).hexdigest()


def command(args, timeout=180, data=None):
    try:
        proc = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or b"", e.stderr or b""


def diag(data):
    lines = []
    for raw in data.decode(errors="replace").splitlines():
        if re.search(r"error|fatal|fail|could not|cannot|missing|no such|not found|no matching|unsupported|no toolchain|permission denied", raw, re.I):
            line = re.sub(r"https?://[^\s)]+", "<url>", raw)
            lines.append(line[:450])
    return {"size": len(data), "sha256": sha(data), "errors": lines[:6] + (lines[-3:] if len(lines)>6 else [])}


def elf(p):
    b = p.read_bytes()
    if b[:6] != b"\x7fELF\x02\x01": raise ValueError("not ELF64 little-endian")
    phoff, = struct.unpack_from("<Q", b, 32)
    sz, count = struct.unpack_from("<HH", b, 54)
    interp = None
    for i in range(count):
        h = phoff+i*sz
        if struct.unpack_from("<I",b,h)[0] == 3:
            off, = struct.unpack_from("<Q",b,h+8)
            size, = struct.unpack_from("<Q",b,h+32)
            interp = b[off:off+size].rstrip(b"\0").decode()
    return {"machine":struct.unpack_from("<H",b,18)[0],"bytes":len(b),"sha256":sha(b),"interpreter":interp}


def option_paths(args, opt):
    out = []
    for i,arg in enumerate(args):
        if arg == opt and i+1<len(args): out.append(args[i+1])
        elif arg.startswith(opt) and len(arg)>len(opt): out.append(arg[len(opt):].lstrip("="))
    return list(dict.fromkeys(out))


def actions(g):
    frags = {int(f["id"]):f for f in g["pathFragments"]}
    def path(i):
        f=frags[int(i)]
        return (path(f["parentId"])+"/" if f.get("parentId") else "")+f["label"]
    arts={int(a["id"]):path(a["pathFragmentId"]) for a in g["artifacts"]}
    owners={int(t["id"]):t["label"] for t in g["targets"]}
    cfg={int(c["id"]):c for c in g.get("configuration",[])}
    rows=[]
    for a in g["actions"]:
        label=owners[int(a["targetId"])]
        if label != TARGET or a["mnemonic"] not in ("CppCompile","CppLink"): continue
        args=list(a.get("arguments",[]));args.extend(v for p in a.get("paramFiles",[]) for v in p.get("arguments",[]))
        outs=[arts[int(k)] for k in a["outputIds"]]
        if a["mnemonic"]=="CppCompile" and "tools/zstd/compressor.c" not in args:continue
        if a["mnemonic"]=="CppLink" and not any(x.endswith("/tools/zstd/compressor") for x in outs):continue
        c=cfg.get(int(a["configurationId"]),{})
        rows.append({"owner":label,"mnemonic":a["mnemonic"],"actionKey":a.get("actionKey"),"cpu":c.get("cpu"),"configuration":c.get("checksum"),"driver":args[0],"triples":sorted({x.split("=")[-1] for x in args if x.split("=")[-1] in ("aarch64-linux-gnu","x86_64-linux-gnu")}),"sysroots":option_paths(args,"--sysroot"),"includes":option_paths(args,"-isystem"),"lib_dirs":option_paths(args,"-L"),"outputs":outs,"argv_sha256":sha(json.dumps(args).encode())})
    return rows


def run_product(argv):
    rc,b,err=command(argv,60,INPUT)
    r={"exit":rc,"stdoutBytes":len(b),"stdoutSha256":sha(b),"diagnostics":diag(err)}
    if rc==0:
        drc,plain,de=command(["zstd","-q","-d","-c"],30,b)
        r.update(decoderExit=drc,inputSha256=sha(INPUT),decodedSha256=sha(plain),match=drc==0 and plain==INPUT and b.startswith(bytes.fromhex("28b52ffd")),decoderDiagnostics=diag(de))
    return r


def arm_execution(root, product, output, rows):
    rc,out,err=command([BAZEL,"cquery","--output=files",QEMU],150)
    q={"qemuQueryExit":rc,"qemuQueryDiagnostics":diag(err)}
    if rc:return q
    paths=out.decode().splitlines()
    if len(paths)!=1 or "qemu_user_prebuilt_linux_amd64_aarch64" not in paths[0]:return q
    emulator=root/paths[0];q.update(qemuPath=paths[0],qemuELF=elf(emulator))
    if q["qemuELF"]["machine"]!=62:return q
    interp=output["interpreter"]
    if not interp:
        q["runtime"]=run_product([str(emulator),str(product)]);return q
    if interp!="/lib/ld-linux-aarch64.so.1":q["unexpectedInterpreter"]=True;return q
    paths=[x for row in rows for k in ("sysroots","lib_dirs","includes") for x in row[k] if not Path(x).is_absolute() and ".." not in Path(x).parts and ("llvm" in x or "glibc" in x)]
    q["examinedToolchainPaths"]=sorted(set(paths)); prefixes=[]
    for x in paths:
        d=root/x
        for prefix in (d,*list(d.parents)[:3]):
            if prefix==root or not prefix.is_relative_to(root):break
            ld=prefix/interp.lstrip("/")
            if ld.is_file() and str(prefix) not in prefixes:prefixes.append(str(prefix))
    q["matchingInterpreterPrefixes"]=[str(Path(p).relative_to(root)) for p in prefixes]
    prefix=Path(prefixes[0]) if prefixes else TASK/"empty-runtime"
    if prefixes:q["loaderELF"]=elf(prefix/interp.lstrip("/"))
    # No host installation or implicit host target-library search path.
    q["hostDefaultTargetLoaderPresent"]=Path(interp).is_file()
    q["runtime"]=run_product([str(emulator),"-L",str(prefix),str(product)])
    return q


def phase(arch, cfg, machine, triple, rec):
    flags=["--config="+cfg,"--extra_toolchains=@llvm//toolchain:all","--jobs=4","--remote_cache=","--remote_executor=","--disk_cache="]
    rec["flags"]=flags
    rc,out,err=command([BAZEL,"aquery",*flags,"--output=jsonproto","--include_param_files",'mnemonic("CppCompile|CppLink", '+TARGET+')'],350)
    rec.update(aqueryExit=rc,aqueryDiagnostics=diag(err))
    rows=[]
    if rc==0:
        try:rows=actions(json.loads(out))
        except (KeyError,IndexError,ValueError) as ex:rec["aqueryReadError"]=str(ex)[:160]
    rec["actions"]=rows
    bpath=TASK/f"{arch}.bep";rc,out,err=command([BAZEL,"build",*flags,"--verbose_failures","--build_event_publish_all_actions","--build_event_json_file="+str(bpath),TARGET],750)
    rec.update(buildExit=rc,buildDiagnostics=diag(err));events=[];completions=[]
    if bpath.exists():
        for line in bpath.read_text().splitlines():
            try:e=json.loads(line)
            except ValueError:continue
            key=e.get("id",{});owner=key.get("actionCompleted",{})
            if owner and (owner.get("label")==TARGET or (not e["action"].get("success") and len(events)<8)):
                events.append({"owner":owner.get("label"),"output":owner.get("primaryOutput"),"configuration":owner.get("configuration",{}).get("id"),"kind":e["action"].get("type"),"success":e["action"].get("success",False),"exitCode":e["action"].get("exitCode",0)})
            t=key.get("targetCompleted",{})
            if t.get("label")==TARGET and not t.get("aspect"):completions.append([t.get("configuration",{}).get("id"),e.get("completed",{}).get("success",False)])
    rec.update(executed=events,completions=completions)
    if rc:return
    # Independently read the normal output even if an optional graph projection failed.
    rc,out,err=command([BAZEL,"cquery",*flags,"--output=files",TARGET]);rec.update(outputQueryExit=rc,outputQueryDiagnostics=diag(err))
    paths=out.decode().splitlines()
    if rc or len(paths)!=1 or not paths[0].endswith("/tools/zstd/compressor"):return
    ir,info,ie=command([BAZEL,"info","execution_root"]);rec.update(execRootExit=ir)
    if ir:return
    root=Path(info.decode().strip());product=root/paths[0];output=elf(product);rec.update(output={"path":paths[0],**output})
    if output["machine"]!=machine:return
    rec["execution"]=run_product([str(product)]) if arch=="amd64" else arm_execution(root,product,output,rows)
    rec["compilerProof"]=len(rows)==2 and all(any(ev["success"] and ev["owner"]==TARGET and ev["kind"]==row["mnemonic"] and ev["output"] in row["outputs"] and ev["configuration"]==row["configuration"] for ev in events) for row in rows) and any(row["mnemonic"]=="CppCompile" and "clang" in row["driver"] and triple in row["triples"] and any("glibc" in x for x in row["includes"]) for row in rows) and any(row["mnemonic"]=="CppLink" and any("glibc" in x for x in row["lib_dirs"]) and paths[0] in row["outputs"] for row in rows) and any(ok for _,ok in completions)


def main():
    assert platform.machine()=="x86_64"
    assert command(["git","rev-parse","HEAD"])[1].decode().strip()==SOURCE
    (TASK/"empty-runtime").mkdir(exist_ok=True)
    r={"source":SOURCE,"worker":platform.machine(),"target":TARGET,"version":command([BAZEL,"--version"])[1].decode().strip(),"arms":{}}
    for arch,cpu,machine,triple in (("amd64","x86_64",62,"x86_64-linux-gnu"),("arm64","aarch64",183,"aarch64-linux-gnu")):
        rec=r["arms"][arch]={}
        try:phase(arch,cpu,machine,triple,rec)
        except (OSError,KeyError,IndexError,ValueError,struct.error) as e:rec["readError"]=str(e)[:250]
        (TASK/"verdict.json").write_text(json.dumps(r,indent=2))
    ok=r["arms"]["amd64"].get("execution",{}).get("match") and all(x.get("compilerProof") for x in r["arms"].values())
    r["crossBuildVerified"]=bool(ok);r["armRuntimeVerified"]=r["arms"]["arm64"].get("execution",{}).get("runtime",{}).get("match",False)
    (TASK/"verdict.json").write_text(json.dumps(r,indent=2))
    return 0 if ok else 1


if __name__=="__main__":
    sys.exit(main())
