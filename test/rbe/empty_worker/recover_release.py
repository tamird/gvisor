"""Recover release products by their original digest; no Bazel invocation."""
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import zipfile

tmp=Path(os.environ['RUNNER_TEMP'])
out=tmp/'release-smoke-result';out.mkdir(exist_ok=True)
cpu=os.environ['EXEC_CPU']
mode=sys.argv[1]
expected_digest={"amd64":"81f97e2c4715343f5839544e62436a8105597e2edf1a8a29249ad3367b3efcb9","arm64":"55a406b910ab4aa47ee21ad2bef3c0d7448ab53aff5d399685f9e40a1a76c6d2"}[cpu]
original=tmp/"original.zip"
assert hashlib.sha256(original.read_bytes()).hexdigest()==expected_digest
with zipfile.ZipFile(original) as z:
    previous=json.loads(z.read("result.json"))
assert previous["verdict"]=="THREE_OPT_DISTRIBUTIONS_SAME_EIGHT_ELFS"
build=previous[cpu]
source=build["source"]
assert source=="21fd3304cb0d31b5cca02c0f3bc75d0c5a53df30" and build["native_exit"]==0
archive=build["packages"]["zstd"]
guest = {'amd64': {'elf': {'sha256': 'dbac288c29ba568459550a2da9e7ae0ded6b1fc728ee9fad3044c44e62d6ac14', 'bytes': 2124608, 'elf_machine': 62}, 'label': '@bootstrap_busybox//:payload/usr/bin/busybox', 'invocation': '3d7a4854-928e-4216-9bbb-b0862de1e133'}, 'arm64': {'elf': {'sha256': '52151e7f322f926b64049cdaa1410dc3ea6485525e0624b05813791c219ae933', 'bytes': 1914704, 'elf_machine': 183}, 'label': '@bootstrap_busybox_arm64//:payload/usr/bin/busybox', 'invocation': 'f211b47b-c357-46ec-9ddc-dafa9dd96e08'}}[cpu]
guest_entry = guest["elf"]
machine,machine_name={"amd64":(62,"x86_64"),"arm64":(183,"aarch64")}[cpu]
inv,=build["invocations"]
def now():
    return datetime.now(timezone.utc).isoformat()
def save():
    (out/'result.json').write_text(json.dumps(results,indent=2)+'\n')
results=json.loads((out/"result.json").read_text()) if mode=="guest" else dict(corrected=source,cpu=cpu,mode="opt",original_artifact_sha256=expected_digest,invocation=inv,archive_sha256=archive["sha256"],archive_bytes=archive["bytes"],started=now())
install=tmp/"exact-release"
busybox=tmp/"original-busybox"
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        return None
def recover(path,entry):
    h,n=entry['sha256'],entry['bytes']
    uri='bytestream://remote.buildbuddy.io/blobs/'+h+'/'+str(n)
    url='https://app.buildbuddy.io/file/download?'+urlencode({'bytestream_url':uri,'invocation_id':inv,'filename':path.name})
    req=Request(url,headers={'x-buildbuddy-api-key':os.environ['BUILDBUDDY_API_KEY']})
    with build_opener(NoRedirect).open(req,timeout=80) as stream, path.open('wb') as destination:
        shutil.copyfileobj(stream,destination)
    with path.open("rb") as stream:
        assert path.stat().st_size==n and hashlib.file_digest(stream,"sha256").hexdigest()==h
    path.chmod(0o755)
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
    sub=results["native_smoke"]={"privileged_host":platform.platform(),"started":now(),"guest":guest["label"],
        "elf":elf(fs/"busybox"),"read_from_guest":expected,"runsc_flags":args[4:],"sidecar_source":"adjacent gvisor-bin extracted from //debian:gvisor-release-tar-zstd"}
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

try:
    assert platform.machine()==machine_name
    assert set(archive["binaries"])=={"runsc","containerd-shim-runsc-v1","gvisor-bin/gvisor_sentry","gvisor-bin/checkpointgofer","gvisor-bin/runsc-metric-server","gvisor-bin/gvisor-sentry-prewarmer","gvisor-bin/runsc-fd-parking","gvisor-bin/gvisor-proc-umounter"}
    if mode=="fetch":
        install.mkdir()
        packed=tmp/"gvisor.tar.zstd"
        recover(packed,archive)
        import tarfile
        recovered={}
        with subprocess.Popen(["zstd","-q","-d","-c",packed],stdout=subprocess.PIPE) as proc:
            with tarfile.open(fileobj=proc.stdout,mode="r|") as tar:
                for member in tar:
                    if not member.isfile(): continue
                    name=member.name.removeprefix("./")
                    assert name in archive["binaries"] and name not in recovered,name
                    dest=install/name
                    dest.parent.mkdir(exist_ok=True)
                    with dest.open("wb") as dst: shutil.copyfileobj(tar.extractfile(member),dst)
                    dest.chmod(member.mode)
                    recovered[name]=elf(dest)
                while proc.stdout.read(1024*1024): pass
            assert proc.wait()==0
        assert recovered==archive["binaries"]
        inv=guest["invocation"]
        try:
            recover(busybox,guest_entry)
            guest_provenance="original CAS"
        except Exception:
            # Same pinned deb and original ELF bytes as the Bazel repository rule.
            package=tmp/"busybox.deb"
            url="https://snapshot.ubuntu.com/ubuntu/20260928T000000Z/pool/main/b/busybox/busybox-static_1.36.1-6ubuntu3.1_"+cpu+".deb"
            subprocess.run(["curl","-fsSL","--retry","2",url,"-o",str(package)],check=True)
            assert hashlib.sha256(package.read_bytes()).hexdigest()=={"amd64":"944b2728f53ceb3916cec2c962873c9951e612408099601751db2a0a5d81e0ed","arm64":"d96535e0402c011e0ee43449799df2f4504d44b842e4f2b3a6cbc845508eaafc"}[cpu]
            target=tmp/"busybox-extracted";subprocess.run(["dpkg-deb","-x",str(package),str(target)],check=True)
            shutil.copy2(target/"usr/bin/busybox",busybox)
            guest_provenance="original target's pinned Ubuntu deb"
        assert elf(busybox)==guest_entry
        results.update(recovered=recovered,guest_provenance=guest_provenance,verified_fetch=now(),verdict="EIGHT_ORIGINAL_ELFS_AND_GUEST_VERIFIED")
    elif mode=="guest":
        assert "BUILDBUDDY_API_KEY" not in os.environ and results["verdict"]=="EIGHT_ORIGINAL_ELFS_AND_GUEST_VERIFIED"
        smoke(install,busybox)
        roots=tmp/"guest-bundle/rootfs"
        results["guest_file_modes"]={name:{"uid":(roots/name).stat().st_uid,"mode":oct(stat.S_IMODE((roots/name).stat().st_mode))} for name in (".","proc","busybox","probe")}
        assert results["native_smoke"]["exit"]==0 and results["native_smoke"]["verdict"]=="GUEST_EXECUTED"
        results["verdict"]="NATIVE_SYSTRAP_WITH_EIGHT_ORIGINAL_SCRATCH_ELFS"
    else: raise ValueError("unexpected mode")
except Exception as error:
    results.update(verdict="INCOMPLETE",error=type(error).__name__+": "+str(error)[:450]);raise
finally:
    results["verified"]=now();save()
    print(cpu,mode,results["verdict"],flush=True)
