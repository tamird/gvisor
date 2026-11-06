"""Install and compare both architectures of the public three-format release."""
from datetime import timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import traceback

import release_guest

SOURCE = "2c90e983df7d4baa575ad8c6ac5a7165a7803029"
TARGET = "//debian:release-archives"
ROOT = Path(os.environ["RUNNER_TEMP"]) / "current-release"
DIST = Path("bazel-bin/debian/release-archives")
VERSION = "20261106." + os.environ["GITHUB_RUN_ID"]
TAG = "release-" + VERSION
FORMATS = ("runsc.deb", "gvisor.tar.bz2", "gvisor.tar.zstd")
FILES = {"runsc", "containerd-shim-runsc-v1"} | {"gvisor-bin/" + n for n in (
    "checkpointgofer", "gvisor_sentry", "gvisor-sentry-prewarmer", "runsc-fd-parking",
    "runsc-metric-server", "runsc-netgofer", "gvisor-proc-umounter",
)}
R = {"source": SOURCE, "target": TARGET, "tag": TAG, "arches": {}, "verified": False}


def save():
    (ROOT / "receipt.json").write_text(json.dumps(R, indent=2) + "\n")


def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1024*1024),b""):
            h.update(block)
    return h.hexdigest()


def regular(path, mode):
    return stat.S_ISREG(path.lstat().st_mode) and stat.S_IMODE(path.stat().st_mode)==mode


def unpack_tar(path, root):
    source = None
    if path.name.endswith(".zstd"):
        source = subprocess.Popen(["zstd", "-dc", path],stdout=subprocess.PIPE)
    stream = tarfile.open(fileobj=source.stdout,mode="r|") if source else tarfile.open(path,"r|bz2")
    seen=set()
    try:
        with stream:
            for member in stream:
                name=member.name.removeprefix("./").rstrip("/")
                if member.isdir():
                    assert name in ("",".","gvisor-bin"), name
                    continue
                assert name in FILES and name not in seen and member.isfile() and 0 < member.size <= 256*1024**2, name
                assert member.mode & 0o7777 == 0o755, name
                seen.add(name)
                dest=root/name
                dest.parent.mkdir(exist_ok=True)
                with stream.extractfile(member) as src, dest.open("xb") as out:
                    shutil.copyfileobj(src,out)
                dest.chmod(member.mode & 0o7777)
        assert seen==FILES
    finally:
        if source:
            source.stdout.close()
            assert source.wait()==0


def deb(path, root, arch, row):
    subprocess.run(["dpkg-deb","--extract",path,root],check=True)
    data_files = {f.relative_to(root).as_posix() for f in root.rglob("*") if f.is_file() or f.is_symlink()}
    expected_files = {"usr/bin/" + name for name in FILES} | {"etc/containerd/runsc.toml"}
    assert data_files == expected_files, (data_files - expected_files, expected_files - data_files)
    ctrl=root.parent/(root.name+"-control")
    subprocess.run(["dpkg-deb","--control",path,ctrl],check=True,umask=0)
    for k,val in (("Package","runsc"),("Architecture",arch),("Version",VERSION)):
        row[k]=subprocess.check_output(["dpkg-deb","-f",path,k],text=True).strip()
        assert row[k]==val,(k,row[k],val)
    row["conffiles"]=(ctrl/"conffiles").read_text().strip().splitlines()
    assert row["conffiles"]==["/etc/containerd/runsc.toml"]
    for target,src,mode in ((ctrl/"postinst",Path("debian/postinst.sh"),0o755),
                            (root/"etc/containerd/runsc.toml",Path("shim/runsc.toml"),0o644)):
        assert regular(target,mode) and target.read_bytes()==src.read_bytes()
        row[target.name]={"sha256":digest(target),"mode":oct(mode)}
    text=(ctrl/"changelog").read_text().splitlines()
    assert text[0]==f"runsc ({VERSION}) unstable; urgency=medium"
    maint=" -- The gVisor Authors <gvisor-dev@googlegroups.com>  "
    assert text[-1].startswith(maint)
    stamp=parsedate_to_datetime(text[-1][len(maint):]); assert stamp.tzinfo
    row["changelog"]={"entry":text[0],"date":stamp.astimezone(timezone.utc).isoformat(),"sha256":digest(ctrl/"changelog")}
    return root/"usr/bin"


def payload(root,machine):
    found={f.relative_to(root).as_posix() for f in root.rglob("*") if f.is_file() or f.is_symlink()}
    assert found==FILES,(found-FILES,FILES-found)
    result={}
    for n in sorted(FILES):
        p=root/n
        with p.open("rb") as f: h=f.read(64)
        assert regular(p,0o755) and h[:7]==b"\x7fELF\x02\x01\x01" and struct.unpack_from("<H",h,18)[0]==machine,n
        result[n]={"sha256":digest(p),"bytes":p.stat().st_size,"mode":"0755","e_machine":machine}
    return result


def report_error(exc):
    return {"class":type(exc).__name__,"line":traceback.extract_tb(exc.__traceback__)[-1].lineno,"detail":str(exc)[:250]}


def check_arch(arch, directory, machine):
    row=R["arches"][arch]={"formats":{}}
    installs={}
    for name in FORMATS:
        p=DIST/directory/name
        data=row["formats"][name]={}
        try:
            data.update(archive_sha256=digest(p),bytes=p.stat().st_size)
            base=ROOT/(arch+"-"+name)
            base.mkdir()
            if name=="runsc.deb":
                bindir=deb(p,base,arch,data)
            else:
                unpack_tar(p,base)
                bindir=base
            data["files"]=payload(bindir,machine)
            data["verified"]=True
            installs[name]=bindir
        except Exception as exc:
            data["error"]=report_error(exc)
        finally:
            save()
    rows=list(row["formats"].values())
    row["payloads_identical"]=all(r.get("verified") for r in rows) and all(r.get("files")==rows[0]["files"] for r in rows[1:])
    save()
    if "runsc.deb" not in installs:
        return
    installed=installs["runsc.deb"]
    cli=installed/"runsc"
    args=[cli,"--version"] if arch=="amd64" else ["qemu-aarch64","-L","/usr/aarch64-linux-gnu",cli,"--version"]
    p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=60)
    row["cli"]={"runner":"native" if arch=="amd64" else "qemu-aarch64","exit":p.returncode,"version":p.stdout.strip()[:250],"error":p.stderr.strip()[:250]}
    row["cli"]["verified"]=p.returncode==0 and "runsc version "+TAG in p.stdout.splitlines()
    save()
    if arch=="amd64":
        busybox=ROOT/"busybox"
        assert regular(busybox,0o755)
        release_guest.SOURCE=TAG
        row["busybox_sha256"]=digest(busybox)
        row["uid"]=os.getuid()
        assert row["uid"]!=0 and os.uname().machine=="x86_64"
        smoke=row["systrap"]=release_guest.launch(ROOT/"guest",installed,busybox)
        smoke["verified"]=smoke["exit"]==0 and all(smoke[key] for key in ("private_root","marker","stdio","sentry","prewarmer","release"))
        save()
    row["verified"]=row["payloads_identical"] and row["cli"]["verified"] and (arch!="amd64" or row["systrap"]["verified"])
    save()


def main():
    save()
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==SOURCE
    assert subprocess.check_output(["git","describe","--tags","--exact-match"],text=True).strip()==TAG
    events=ROOT/"build.bep"
    args=[ROOT/"bazel","--nosystem_rc","--nohome_rc","build","-c","opt","--jobs=4","--keep_going","--color=no",
          "--noshow_progress","--build_event_json_file="+str(events),TARGET]
    R["build_exit"]=subprocess.run(args).returncode
    R["success_event"]=False
    if events.exists():
        errors=[]
        with events.open() as f:
            for line in f:
                ev=json.loads(line); k=ev["id"].get("targetCompleted",{})
                if k.get("label")==TARGET and "completed" in ev and not k.get("aspect"):
                    R["success_event"]=ev["completed"].get("success",False)
                a=ev.get("action")
                if a and not a.get("success",False) and len(errors)<20:
                    errors.append({"label":ev["id"].get("actionCompleted",{}).get("label"),"type":a.get("type"),"exit":a.get("exitCode")})
        R["failed_actions"]=errors
    R["outputs"]=sorted(str(p.relative_to(DIST)) for p in DIST.glob("*/*") if p.is_file())
    save()
    for arch,directory,machine in (("amd64","x86_64",62),("arm64","aarch64",183)):
        try:check_arch(arch,directory,machine)
        except Exception as exc:
            R["arches"].setdefault(arch,{})["error"]=report_error(exc);save()
    R["verified"]=R["build_exit"]==0 and R["success_event"] and len(R["outputs"])==6 and all(r.get("verified") for r in R["arches"].values())
    save()
    return not R["verified"]


if __name__=="__main__":
    sys.exit(main())
