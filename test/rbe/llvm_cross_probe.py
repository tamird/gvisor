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
import tarfile

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


INDEX = "ec06c3b6cea04192ae6770c434f668ca41d343ad19fa6472216c7b48be39c598"
MANIFEST = "bf9d5b1f01be2de7cec5aa6e3977e8848f7ab9a307c6c3a8af7a50a27e609023"
LAYER = "df9831efb49cab24dec437e8482f2a9ddbd9e239131fa3ffe095dc1492081e1d"
ORIGINAL = "6f473c93e7c72b1e9df199da15aa058acdf280240021c3eaf4d04cecc7ff54ed"
LIBRARIES = {
    "ld-linux-aarch64.so.1": "1b882eddacd494b060413a8aa4f0a4754ee3f84775e68cecbf8a86092b59cca6",
    "libc.so.6": "70fa4c4cf57eabeb96097cce47582d02379c4cd1de3db3e98ade4dd7089a1b13",
    "libdl.so.2": "8023ba7cb6ecabb57d1910ecd24ce347cba5653d1b547d9c7950301ed59716ca",
    "libm.so.6": "a8afb2d1f4a0c5e14c0fc82f794ec839eafe29738907aa92895410f57630bbe2",
    "libpthread.so.0": "b4f0bcbb029e42861545757592356a490d87ef42cfa8a7f2d4295846e42698b5",
    "libgcc_s.so.1": "9a146967dc38c3820c5dc21c3209812655f66b6551264072b187bd0e787947e0",
}


def arm_execution(root, product, rec):
    q=rec["execution"]={"fixture":"@test_bun","index":INDEX,"manifest":MANIFEST,"layer":LAYER}
    rc,out,err=command([BAZEL,"info","output_base"]);assert rc==0
    base=Path(out.decode().strip())
    rc,out,err=command([BAZEL,"build",QEMU],240);q.update(qemuBuildExit=rc,qemuBuildDiagnostics=diag(err))
    if rc:return
    rc,out,err=command([BAZEL,"cquery","--output=files",QEMU]);q.update(qemuQueryExit=rc,qemuQueryDiagnostics=diag(err))
    paths=out.decode().splitlines()
    if rc or len(paths)!=1: return
    rel=Path(paths[0])
    assert rel.parts==("external","rules_qemu++qemu+qemu_user_prebuilt_linux_amd64_aarch64","qemu-user-linux-amd64-aarch64")
    emulator=next(p for p in (base/rel,root/rel) if p.is_file())
    q.update(qemuPath=str(rel),qemuELF=elf(emulator))
    assert q["qemuELF"]["machine"]==62 and q["qemuELF"]["interpreter"] is None
    qr,ver,err=command([str(emulator),"--version"]);q.update(qemuVersionExit=qr,qemuVersion=ver.decode().splitlines()[:1]);assert qr==0
    rc,out,err=command([BAZEL,"query","--output=location","@test_bun//:image"],420)
    q.update(fixtureQueryExit=rc,fixtureDiagnostics=diag(err))
    if rc:return
    candidates=[p for p in (base/"external").glob("*+test_bun") if (p/"blobs/sha256"/INDEX).is_file()]
    assert len(candidates)==1, "source-declared test_bun repository not materialized"
    blobs=candidates[0]/"blobs/sha256"
    def blob(digest,size):
        p=blobs/digest
        assert p.stat().st_size<=size and sha(p.read_bytes())==digest
        return p
    index=json.loads(blob(INDEX,262144).read_bytes());manifest=json.loads(blob(MANIFEST,262144).read_bytes())
    assert any(x["digest"]=="sha256:"+MANIFEST and x["platform"]["architecture"]=="arm64" and x["platform"]["os"]=="linux" for x in index["manifests"])
    assert manifest["layers"][0]["digest"]=="sha256:"+LAYER and manifest["layers"][0]["size"]==49761105
    ar=blob(LAYER,49761105);assert ar.stat().st_size==49761105
    lib=TASK/"arm-gnu-runtime"/"lib";lib.mkdir(parents=True)
    q["members"]={}
    with tarfile.open(ar,"r|gz") as tar:
        for entry in tar:
            name=entry.name.rsplit("/",1)[-1]
            if name not in LIBRARIES or entry.name!="usr/lib/aarch64-linux-gnu/"+name:continue
            assert entry.isfile() and 0<entry.size<3000000 and name not in q["members"]
            b=tar.extractfile(entry).read()
            assert len(b)==entry.size and sha(b)==LIBRARIES[name]
            with (lib/name).open("xb") as w:w.write(b)
            (lib/name).chmod(0o555);q["members"][name]=elf(lib/name)
    assert set(q["members"])==set(LIBRARIES) and all(e["machine"]==183 for e in q["members"].values())
    rc,out,err=command(["readelf","-dW",str(product)]);assert rc==0
    needed=re.findall(r"\(NEEDED\).*?\[([^]]+)\]",out.decode());q["needed"]=needed
    assert needed and set(needed)<=set(LIBRARIES)
    ld=lib/"ld-linux-aarch64.so.1"
    cmd=[str(emulator),"-L",str(lib.parent),str(ld),"--inhibit-cache","--library-path",str(lib)]
    rc,out,err=command(cmd+["--list",str(product)])
    q.update(loaderListExit=rc,loaderListDiagnostics=diag(err))
    resolved={n:bool(re.search(r"=> "+re.escape(str(lib/n))+r"(?:\s|$)",out.decode())) for n in needed}
    q["fixtureResolved"]=resolved
    if rc or not all(resolved.values()):return
    q["runtime"]=run_product(cmd+[str(product)])

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
    rec["compilerProof"]=len(rows)==2 and all(any(ev["success"] and ev["owner"]==TARGET and ev["kind"]==row["mnemonic"] and ev["output"] in row["outputs"] and ev["configuration"]==row["configuration"] for ev in events) for row in rows) and any(row["mnemonic"]=="CppCompile" and "clang" in row["driver"] and triple in row["triples"] and any("glibc" in x for x in row["includes"]) for row in rows) and any(row["mnemonic"]=="CppLink" and any("glibc" in x for x in row["lib_dirs"]) and paths[0] in row["outputs"] for row in rows) and any(ok for _,ok in completions)
    rec["matchesOriginalELF"]=output["sha256"]==ORIGINAL and output["bytes"]==1612928
    if rec["matchesOriginalELF"]:
        arm_execution(root,product,rec)


def main():
    assert platform.machine()=="x86_64"
    assert command(["git","rev-parse","HEAD"])[1].decode().strip()==SOURCE
    (TASK/"empty-runtime").mkdir(exist_ok=True)
    r={"source":SOURCE,"worker":platform.machine(),"target":TARGET,"version":command([BAZEL,"--version"])[1].decode().strip(),"arms":{}}
    r["original"]={"run":42078042318,"artifact":13439397539,"zipSha256":"3e4f1075d366ac91c46a0b542a5543ddb407ea6fd65afda2f203ec111152da8c"}
    rec=r["arms"]["arm64"]={}
    try:phase("arm64","aarch64",183,"aarch64-linux-gnu",rec)
    except (OSError,KeyError,IndexError,ValueError,AssertionError,StopIteration,struct.error,tarfile.TarError) as e:
        rec["readError"]=type(e).__name__+": "+str(e)[:250]
    run=rec.get("execution",{}).get("runtime",{})
    r["armRuntimeVerified"]=bool(rec.get("compilerProof") and rec.get("matchesOriginalELF") and run.get("match") and run.get("stdoutSha256")=="aa0c4f7ead3f3c5e323bf02eb2efd550ce567c92a1674395f61944125c40547a")
    (TASK/"verdict.json").write_text(json.dumps(r,indent=2))
    return 0 if r["armRuntimeVerified"] else 1


if __name__=="__main__":
    sys.exit(main())
