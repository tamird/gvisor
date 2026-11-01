"""Smoke the verified AMD64 release in a separate privileged native guest."""
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess

from qualify import now, save as store, PRODUCT, TMP

tmp = TMP
cpu = "amd64"
machine = 62
machine_name = "x86_64"
guest = "@bootstrap_busybox//:payload/usr/bin/busybox"
results = {"corrected": PRODUCT}

def save():
    store("guest",results)

def elf(path):
    with path.open("rb") as stream:
        head=stream.read(24)
        assert head[:6]==b"\x7fELF\x02\x01" and int.from_bytes(head[18:20],"little")==machine
        stream.seek(0)
        return {"sha256":hashlib.file_digest(stream,"sha256").hexdigest(),"bytes":path.stat().st_size,"elf_machine":machine}
def smoke(install,busybox):
    bundle=tmp/"guest-bundle"; fs=bundle/"rootfs"; fs.mkdir(parents=True)
    (fs/"proc").mkdir()
    shutil.copy2(busybox,fs/"busybox")
    (fs/"busybox").chmod(0o755)
    expected="release-"+cpu+"-"+results["corrected"][:9]
    (fs/"probe").write_text(expected)
    # The runner owns these files. Guest uid 0 has no DAC override in this spec.
    for child in (fs,fs/"proc"): child.chmod(0o755)
    (fs/"probe").chmod(0o644)
    process={"terminal":False,"user":{"uid":0,"gid":0},"cwd":"/",
             "args":["/busybox","sh","-ec",'/busybox cat /probe; printf "\\n"; /busybox uname -m'],"env":["PATH=/"]}
    spec={"ociVersion":"1.0.2","root":{"path":"rootfs"},"process":process,
          "mounts":[{"type":"proc","source":"proc","destination":"/proc"}],
          "linux":{"namespaces":[{"type":n} for n in ("pid","mount","uts","ipc","network")]}}
    (bundle/"config.json").write_text(json.dumps(spec))
    state=tmp/"oci-state";state.mkdir()
    apparmor=Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
    if apparmor.exists() and apparmor.read_text().strip()!="0":
        subprocess.run(["sudo","sysctl","-w","kernel.apparmor_restrict_unprivileged_userns=0"],check=True)
    args=["sudo","-n","--",str(install/"runsc"),"--root="+str(state),"--platform=systrap","--network=none",
          "--overlay2=none","--host-settings=ignore","--ignore-cgroups"]
    cid="scratch-release"
    sub=results["native_smoke"]={"privileged_host":platform.platform(),"started":now(),"guest":guest,
        "elf":elf(fs/"busybox"),"read_from_guest":expected,"runsc_flags":args[4:],"sidecar_source":"adjacent gvisor-bin from //:release"}
    save()
    try:
        proc=subprocess.run(["timeout","--kill-after=5","90",*args,"run","--bundle="+str(bundle),cid],
                            stdin=subprocess.DEVNULL,capture_output=True,text=True)
        sub.update(exit=proc.returncode,stdout=proc.stdout[:1000],stderr=proc.stderr[-5000:],finished=now())
        assert proc.returncode==0 and proc.stdout.splitlines()==[expected,machine_name], "guest did not emit both expected outputs"
        sub["verdict"]="GUEST_EXECUTED"
    finally:
        subprocess.run(["timeout","20",*args,"delete","--force",cid],capture_output=True)
        save()
