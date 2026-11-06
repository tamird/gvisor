import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tarfile
import traceback
import uuid

D=Path(os.environ["RUNNER_TEMP"])/"writer-runtime"
REPORT=D/"selection.json"
SRC={"control":"bbbed5b2b84fe21597c740789d841e618f912def","registered":"bb983b988802b854bd03dd7a0a5220e7e7b4440e"}
PLATFORM="@llvm//platforms:linux_"
SPECS={"x86_64":(62,"amd64","/lib64/ld-linux-x86-64.so.2","d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"),
       "aarch64":(183,"arm64","/lib/ld-linux-aarch64.so.1","b0aca6ddfad947e66782a29534647936d4d645a1b96d9c8a97e4f53b1607d428")}
CASES=[("cross-x86-musl","x86_64","musl","aarch64","glibc_static"),("cross-arm-musl","aarch64","musl","x86_64","glibc_static"),
       ("cross-x86-glibc","x86_64","glibc_static","aarch64","glibc_static"),("cross-arm-glibc","aarch64","glibc_static","x86_64","glibc_static"),
       ("normal","x86_64","ordinary","x86_64","ordinary"),("target-musl","x86_64","glibc_static","aarch64","musl")]
PKG="//shim:config"
BZ2="@rules_pkg//tests/tar:test-tar-compression-from-extension-bz2"
WRITER="@rules_pkg//pkg/private/tar:build_tar"
EXPR='filter("rules_pkg.*//pkg/private/tar:build_tar$",deps('+PKG+')) union '+WRITER
BASE=[str(D/"bazel"),"--nosystem_rc","--nohome_rc"]
def save():
    REPORT.write_text(json.dumps(report,indent=2)+"\n")
def head(source):
    subprocess.run(["git","checkout","--quiet","--detach",source],check=True)
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==source
    assert not subprocess.check_output(["git","diff","--name-only","HEAD"])
def flags(ex,lib,tgt,tlib):
    args=["--config="+tgt,"-c","opt","--lockfile_mode=off","--noshow_progress","--color=no","--curses=no"]
    if lib!="ordinary":
        ep=PLATFORM+ex+"_"+lib;args+=["--extra_execution_platforms="+ep,"--host_platform="+ep]
    if tlib!="ordinary":args+=["--platforms="+PLATFORM+tgt+"_"+tlib]
    return args
def invoke(rec,args,stem):
    cmd=next(i for i,a in enumerate(args) if a in ("cquery","build"))
    iid=str(uuid.uuid4());rec["invocation"]=iid;save()
    err=D/(stem+".stderr");out=D/(stem+".stdout")
    with err.open("wb") as e,out.open("wb") as o:
        p=subprocess.run(["timeout","-k","20","800",*args[:cmd+1],"--invocation_id="+iid,*args[cmd+1:]],stdout=o,stderr=e)
    raw=err.read_bytes()
    rec["exit"]=p.returncode;rec["diagnostic"]=[s.decode() for s in (b"no such package",b"no such target",b"no matching toolchain",b"could not resolve",b"not a valid",b"no such file",b"checksum",b"fail: ",b"error downloading",b"starlark evaluation error",b"no registered",b"unknown",b"module extension",b"to extract",b"libpython",b"no version") if s in raw.lower()]
    if cmd==len(BASE) and p.returncode: print(raw[-2600:].decode(errors="replace"),flush=True)
    save();return out
def elf(path):
    with path.open("rb") as f:buf=f.read(100*1024*1024+1)
    assert len(buf)<100*1024*1024 and buf[:6]==b"\x7fELF\x02\x01"
    ph=struct.unpack_from("<Q",buf,32)[0];width,n=struct.unpack_from("<HH",buf,54);interp=None
    for i in range(n):
        typ,_,off,_,_,num,_,_=struct.unpack_from("<II6Q",buf,ph+i*width)
        if typ==3:
            interp=buf[off:off+num].rstrip(b"\0").decode()
            assert re.fullmatch(r"/[a-zA-Z0-9._/+~-]+",interp)
    return {"arch":struct.unpack_from("<H",buf,18)[0],"interp":interp,"sha256":hashlib.sha256(buf).hexdigest()}
def select():
    head(SRC["registered"])
    report["original"]={"run":42191023408,"artifact":13490336305,"sha256":"1105866eaf6b8b9c0934b9ecc39522090eb35429ce9e3f24d259fa0c3ed6e195","control":SRC["control"],"registered":SRC["registered"]}
    output_base=Path(subprocess.check_output([*BASE,"info","output_base"],text=True,stderr=subprocess.DEVNULL).strip())
    inspected={}
    complete=True
    for case,ex,lib,tgt,tlib in [CASES[0],CASES[1],CASES[5]]:
        rec=report.setdefault("configured",{}).setdefault(case,{})
        out=invoke(rec,[*BASE,"cquery",*flags(ex,lib,tgt,tlib),"--output=starlark","--starlark:file="+str(D/"writer_runtime.cquery"),EXPR],case)
        if rec["exit"]:continue
        inputs={}
        for row in out.read_text().splitlines():
            info=json.loads(row)
            kind="exec" if any(v.startswith("bazel-out/") and "-exec" in v.split("/")[1] for v in info["outputs"]) else "target"
            arch,libc=(ex,lib) if kind=="exec" else (tgt,tlib)
            wanted="gvisor-"+arch+"-linux-musl-static" if libc=="musl" else arch+"-unknown-linux-gnu"
            candidate,=[v for v in info["interpreters"] if v.endswith("/bin/python3")]
            root=Path(candidate).parts
            assert len(root)==4 and root[0]=="external" and root[-2]=="bin" and re.fullmatch(r"[A-Za-z0-9_+.-]+",root[1])
            assert kind not in inputs
            inputs[kind]=candidate
            slot=rec[kind]={"repo":root[1],"expected":"rules_python++python+python_3_11_"+wanted,"encoding":any(v.startswith("external/"+root[1]+"/") for v in info["encodings"])}
        save()
        for kind,rel in inputs.items():
            target_data=rec[kind];path=output_base/rel
            if not path.is_file():
                target_data["missing_canonical_file"]=rel;complete=False;continue
            try:
                if rel not in inspected:inspected[rel]=elf(path)
                target_data.update(inspected[rel])
            except (OSError,ValueError,AssertionError) as e:
                target_data["file_error"]=type(e).__name__;complete=False
        save()
    for case,ex,lib,tgt,tlib in [CASES[0],CASES[1],CASES[5]]:
        rec=report["configured"][case]
        for kind,arch,libc in (("exec",ex,lib),("target",tgt,tlib)):
            row=rec[kind];assert row["repo"]==row["expected"] and row["encoding"]
            if "arch" in row:
                assert row["arch"]==SPECS[arch][0] and row["interp"]==(None if libc=="musl" else SPECS[arch][2])
                if libc=="musl" and arch=="x86_64":assert row["sha256"]=="02939080d36c0fd2dc2507ff146a4c86360fa3968f30e1e42a6424d352629394"
    report["selection_status"]="PASS" if complete else "SELECTED";save()

def packages():
    assert report["selection_status"] in ("PASS","SELECTED")
    head(SRC["registered"])
    for case,ex,lib,tgt,tlib in CASES[:2]:
        rec=report.setdefault("packages",{}).setdefault(case,{})
        cpu=SPECS[ex][1];image=SPECS[ex][3]
        log=D/(case+".exec.json");opt=flags(ex,lib,tgt,tlib)
        events=D/(case+".events.json");save()
        invoke(rec,[*BASE,"--bazelrc="+str(D/"private.rc"),"build",*opt,"--@rules_python//python/config_settings:bootstrap_impl=native",
                        "--remote_default_exec_properties=Arch="+cpu,"--remote_default_exec_properties=OSFamily=linux",
                        "--remote_default_exec_properties=workload-isolation-type=oci","--remote_default_exec_properties=recycle-runner=false",
                        "--remote_default_exec_properties=container-image=docker://gcr.io/distroless/static-debian13@sha256:"+image,
                        "--modify_execution_info=^PackageTar$=+no-remote-cache","--execution_log_json_file="+str(log),
                        "--build_event_json_file="+str(events),"--remote_download_outputs=toplevel",PKG,BZ2],case+"-package")
        failures=[]
        if events.exists():
            for line in events.open():
                event=json.loads(line);action=event.get("action",{})
                for item in event.get("namedSetOfFiles",{}).get("files",[]):
                    nm=item.get("name",""); h=item.get("digest",""); size=item.get("length","")
                    if (nm.endswith("shim/config.tar") or nm.endswith("test-tar-compression-from-extension-bz2.bz2")) and re.fullmatch(r"[0-9a-f]{64}",h) and str(size).isdigit():
                        rec.setdefault("outputs",{})[nm.split("/")[-1]]={"digest":h,"size":int(size)}
                if action.get("success",True) is not False:continue
                digest=re.search(r"/blobs/([0-9a-f]{64})/([0-9]+)$",action.get("stderr",{}).get("uri",""))
                if digest and len(failures)<4:failures.append({"mnemonic":action.get("type",""),"stderr":digest.group(1),"size":int(digest.group(2))})
        rec["failure_actions"]=failures;save()
        if rec["exit"]:continue
        try:
            data=subprocess.check_output(["jq","-sc",'[.[]|if type=="array" then .[] else . end|select(.mnemonic=="PackageTar")|{owner:.targetLabel,runner,exit:(.exitCode//0),cached:(.cacheHit//false),props:((.platform.properties//[])|map({key:.name,value:.value})|from_entries),writer:.commandArgs[0]}]',log])
            rows=json.loads(data)
            rec["actions"]=[{"owner":a["owner"], "runner":a["runner"], "exit":a["exit"], "cached":a["cached"],"arch":a["props"].get("Arch"),"image":a["props"].get("container-image")} for a in rows];save()
            owners=sorted(a["owner"] for a in rows)
            rec["owners"]=owners;save()
            assert PKG in owners and any(v.endswith("//tests/tar:test-tar-compression-from-extension-bz2") for v in owners) and len(owners)==2
            for item in rows:
                assert item["runner"]=="remote" and not item["cached"] and item["exit"]==0
                assert item["props"]["Arch"]==cpu and item["props"]["container-image"].endswith(image)
                assert item["writer"].endswith("/pkg/private/tar/build_tar")
            rec["worker"]={"cpu":cpu,"image":image,"uncached":True}
            builddir=Path("bazel-bin/external");archive,=list(builddir.glob("rules_pkg*/tests/tar/test-tar-compression-from-extension-bz2*.bz2"))
            assert archive.read_bytes()[:3]==b"BZh"
            with tarfile.open(archive,"r:bz2") as tf:
                member,=[m for m in tf if m.isfile()];assert Path(member.name).name=="loremipsum.txt"
                packed=tf.extractfile(member).read()
            base=Path(subprocess.check_output([*BASE,"info","output_base"],text=True,stderr=subprocess.DEVNULL).strip())
            data_in,=list((base/"external").glob("rules_pkg*/tests/testdata/loremipsum.txt"))
            assert packed==data_in.read_bytes()
            rec["bz2"]={"payload_sha256":hashlib.sha256(packed).hexdigest(),"tar_sha256":hashlib.sha256(archive.read_bytes()).hexdigest()};save()
            archive=Path("bazel-bin/shim/config.tar")
            with tarfile.open(archive) as tf:
                member,=[m for m in tf if m.isfile()]
                assert member.name.lstrip("./")=="etc/containerd/runsc.toml" and member.mode&0o7777==0o644
                raw=tf.extractfile(member).read()
                assert raw==Path("shim/runsc.toml").read_bytes()
                rec.update(payload=hashlib.sha256(raw).hexdigest(),mode=member.mode,passed=True)
            save()
        except Exception as err:
            rec["report_error"]={"type":type(err).__name__,"line":traceback.extract_tb(err.__traceback__)[-1].lineno};save()
    assert all(v.get("passed") for v in report["packages"].values())
    report["package_status"]="PASS"

mode=sys.argv[1];report={"selection_status":"INCOMPLETE","package_status":"PENDING"} if mode=="select" else json.loads(REPORT.read_bytes())
try:(select if mode=="select" else packages)()
except Exception as e:
    report[mode+"_error"]={"type":type(e).__name__,"line":traceback.extract_tb(e.__traceback__)[-1].lineno}
    raise
finally:save()
