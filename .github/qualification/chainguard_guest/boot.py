"""Run the established native systrap guest against the exact installed ELF set."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import sys
import tarfile
import traceback

import runtime_guest as guest

WORK = Path(os.environ["RUNNER_TEMP"]) / "chainguard-guest"
ORIGINAL = json.loads((WORK/"expected.json").read_text())
FETCH = json.loads((WORK/"fetch.json").read_text())
ARCH, = sys.argv[1:]
CPU, ELF = {"amd64": ("x86_64", 62), "arm64": ("aarch64", 183)}[ARCH]
EXPECTED = ORIGINAL["arms"][ARCH]
ROW = {"source": ORIGINAL["source"], "invocation": ORIGINAL["invocation"], "originalRun": ORIGINAL["originalRun"],
       "arch": ARCH, "hostCPU": platform.machine(), "platform":"systrap","rootless":True,"network":"none",
       "archive":EXPECTED["archive"],"installed":{},"verified":False}
MEMBERS = {"runsc","containerd-shim-runsc-v1"} | {"gvisor-bin/"+s for s in
    ("gvisor_sentry","checkpointgofer","gvisor-sentry-prewarmer","runsc-fd-parking","runsc-metric-server","runsc-netgofer","gvisor-proc-umounter")}


def filehash(p):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda:f.read(1<<20),b""): h.update(chunk)
    return h.hexdigest()


def install(archive, dest):
    with tarfile.open(archive, "r|bz2") as t:
        for member in t:
            name=member.name.removeprefix("./").rstrip("/")
            if member.isdir():
                assert name in ("",".","gvisor-bin")
                continue
            assert name in MEMBERS and name not in ROW["installed"] and member.isreg() and member.mode & 0o7777 == 0o755
            expected=EXPECTED["files"][name]
            assert member.size==expected["bytes"] and 0<member.size<180<<20
            out=dest/name
            out.parent.mkdir(parents=True,exist_ok=True)
            with out.open("xb") as f:
                source=t.extractfile(member)
                for chunk in iter(lambda:source.read(1<<20),b""):f.write(chunk)
            out.chmod(0o755)
            with out.open("rb") as f:head=f.read(64)
            assert not out.is_symlink() and stat.S_IMODE(out.stat().st_mode)==0o755 and head[:6]==b"\x7fELF\x02\x01"
            observed={"sha256":filehash(out),"bytes":out.stat().st_size,"mode":0o755,"e_machine":int.from_bytes(head[18:20],"little")}
            assert observed==expected and observed["e_machine"]==ELF
            ROW["installed"][name]=observed
    assert set(ROW["installed"])==MEMBERS


def run():
    assert platform.machine()==CPU and os.geteuid()!=0
    assert not {"BUILDBUDDY_API_KEY","ACCESS","GITHUB_TOKEN","GH_TOKEN"}.intersection(os.environ)
    archive=WORK/"original.tar.bz2"
    assert FETCH["verified"] and FETCH["invocation"]==ORIGINAL["invocation"]
    assert FETCH["observedSHA256"]==EXPECTED["archive"]["sha256"]==filehash(archive)
    assert FETCH["receivedBytes"]==EXPECTED["archive"]["bytes"]==archive.stat().st_size
    ROW["stage"]="install"
    dest=WORK/"installed"
    dest.mkdir()
    install(archive,dest)
    ROW["stage"]="fixture"
    guest.q.TASK=guest.r.TASK=WORK
    guest.r.SOURCE=ORIGINAL["source"];guest.MEMBERS=MEMBERS
    fixture={}
    binary=guest.fixture(fixture,ARCH)
    ROW["staticBusyBox"]={"sha256":filehash(binary),"packageSHA256":fixture["package_sha256"],"machine":fixture["elf"]["machine"]}
    capsule,marker=guest.capsule("capsule",binary,dest)
    # Keep the original namespace, Sentry, sidecar, and release checks. Have
    # the guest itself identify its architecture after the original marker.
    p=capsule/"bundle/config.json"; cfg=json.loads(p.read_text())
    cfg["process"]["args"]=["/busybox","sh","-ec",'/busybox echo "$1"; exec /busybox uname -m',"guest",marker]
    p.write_text(json.dumps(cfg))
    sentry="gvisor-bin/gvisor_sentry"
    ROW["Sentry"]={"path":"/release/"+sentry,"sha256":ROW["installed"][sentry]["sha256"],
                   "sameInodeInCapsule":(capsule/"release"/sentry).samefile(dest/sentry)}
    ROW["stage"]="guest"
    smoke={};stdout=guest.launch(capsule,smoke)
    ROW["guest"]={k:smoke.get(k) for k in ("exit","stdout_sha256","private_root_entered","sentry_stdio_witness",
                  "prewarmer_selected","sentry_selected","sentry_release_matches")}
    ROW["guest"]["stderrSHA256"]=smoke.get("stderr",{}).get("sha256")
    lines=" ".join(smoke.get("stderr",{}).get("lines",[])).lower()
    ROW["guest"]["diagnostics"]=[term for term in ("not permitted","permission denied","no such file","invalid argument",
                                  "exec format error","timed out","unknown flag","release version","operation not supported") if term in lines]
    out=stdout.splitlines()
    ROW["guest"]["marker"]=bool(out and out[0]==marker.encode())
    ROW["guest"]["cpu"]=out[1].decode() if len(out)==2 and out[1] in (b"x86_64",b"aarch64") else None
    ROW["verified"]=bool(out==[marker.encode(),CPU.encode()] and smoke["exit"]==0 and
        ROW["Sentry"]["sameInodeInCapsule"] and all(smoke.get(k) for k in (
            "private_root_entered","sentry_selected","sentry_release_matches","sentry_stdio_witness")))
    ROW["stage"]="finished"

try:run()
except (AssertionError,OSError,ValueError,KeyError,tarfile.TarError) as e:
    ROW["error"]=type(e).__name__
    ROW["line"]=traceback.extract_tb(e.__traceback__)[-1].lineno
finally:(WORK/"runtime.json").write_text(json.dumps(ROW,sort_keys=True,indent=2)+"\n")
raise SystemExit(0 if ROW["verified"] else 1)
