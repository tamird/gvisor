"""Exercise the installed runtime's PCAP on one or two real interfaces."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import socket
import struct
import subprocess
import tempfile
import traceback
import xml.etree.ElementTree as ET

def run(*args):
    completed = subprocess.run(args, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(f"{args}: {completed.returncode}: {completed.stderr}")
    return completed.stdout


def unmount_null_netns(path):
    # runsc shares a pinned null gofer namespace below --root even after the
    # container is deleted. Detach it before removing this lab's runtime root.
    while path.exists() and subprocess.run(["mountpoint", "-q", path]).returncode == 0:
        run("umount", "-l", path)


def spawn(scope, args):
    process = subprocess.Popen(args, stdout=subprocess.PIPE, text=True)
    def stop():
        if process.poll() is None:
            process.kill()
        process.wait()
    scope.callback(stop)
    return process


def ready(process):
    if not select.select([process.stdout], [], [], 25)[0]:
        raise TimeoutError(f"startup timed out: {process.args}")
    if "ready" not in (line := process.stdout.readline()):
        raise RuntimeError(f"startup failed: {line!r} ({process.args})")


def packets(path, ips):
    data = path.read_bytes()
    if struct.unpack_from("<IHH",data) != (0xa1b2c3d4,2,4) or struct.unpack_from("<II",data,16) != (4096,101):
        raise ValueError("missing 4096-byte native RAW pcap header")
    offset = 24
    rows = {}
    packets = 0
    while offset < len(data):
        if data[offset:offset+4] == bytes.fromhex("d4c3b2a1"):
            raise ValueError("second global pcap header")
        seconds,micros,captured,original = struct.unpack_from("<IIII",data,offset)
        offset += 16
        if seconds == 0 or micros >= 1000000 or captured != min(original,4096) or offset+captured > len(data):
            raise ValueError("invalid pcap record")
        ip = data[offset:offset+captured]
        offset += captured
        packets += 1
        if len(ip) < 20 or ip[0] >> 4 != 4 or ip[9] != 1:
            continue
        h = 4*(ip[0]&15)
        if len(ip) < h+8 or ip[h] not in (0,8):
            continue
        src,dst=socket.inet_ntoa(ip[12:16]),socket.inet_ntoa(ip[16:20])
        icmpid,seq=struct.unpack_from("!HH",ip,h+4)
        key = (src,dst) if ip[h] == 8 else (dst,src)
        rows.setdefault(key,{}).setdefault((icmpid,seq),set()).add(ip[h])
    ok = {}
    for src,dst in ips:
        ok[f"{src}->{dst}"] = [[i,s] for (i,s),types in rows.get((src,dst),{}).items() if types=={0,8}]
    if not all(ok.values()):
        raise ValueError(f"missing captured ICMP request/reply: {ok}")
    return dict(bytes=len(data), records=packets, headers=1, echoes=ok)


def case(runtime, helper, work, links):
    work.mkdir()
    guest = "pcap-guest"
    ips = []
    cap = work / "guest.pcap"
    with contextlib.ExitStack() as scope:
        for name in [guest]+[f"pcap-peer{i}" for i in range(links)]:
            run("ip","netns","add",name)
            scope.callback(subprocess.run,["ip","netns","del",name],check=False)
            run("ip","-n",name,"link","set","lo","up")
        for i in range(links):
            peer,dev,gdev=f"pcap-peer{i}",f"vp{i}",f"vg{i}"
            remote,local=f"10.172.{i}.2",f"10.172.{i}.1"
            ips.append((remote,local))
            run("ip","link","add",gdev,"netns",guest,"type","veth","peer","name",dev,"netns",peer)
            for ns,device,address in ((guest,gdev,local),(peer,dev,remote)):
                run("ip","-n",ns,"addr","add",address+"/30","dev",device)
                run("ip","-n",ns,"link","set",device,"up")
        fs=work/"rootfs";fs.mkdir();(fs/"proc").mkdir();shutil.copy2(helper,fs/"init")
        config=dict(ociVersion="1.0.2",root=dict(path="rootfs",readonly=True),
                    process=dict(args=["/init"],cwd="/",user=dict(uid=0,gid=0),capabilities={k:["CAP_NET_ADMIN"] for k in ("effective","permitted","bounding")}),
                    mounts=[dict(destination="/proc",type="proc",source="proc")],linux=dict(namespaces=[dict(type=k) for k in ("pid","ipc","uts","mount")]+[dict(type="network",path="/var/run/netns/"+guest)]))
        (work/"config.json").write_text(json.dumps(config))
        base=[str(runtime),"--root="+str(work/"state"),"--platform=systrap","--ignore-cgroups=true","--gso=false","--pcap-log="+str(cap)]
        scope.callback(unmount_null_netns,work/"state"/"null-netns")
        scope.callback(subprocess.run,[*base,"delete","--force","guest"],check=False)
        ready(spawn(scope,[*base,"run","--bundle",work,"guest"]))
        for i,(_,addr) in enumerate(ips):
            run("ip","netns","exec",f"pcap-peer{i}","ping","-n","-c","1","-W","3",addr)
    return packets(cap,ips)


def main():
    records=Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    base=Path(os.environ["TEST_SRCDIR"])/os.environ["TEST_WORKSPACE"]
    modes=os.environ["PCAP_CASES"].split(",")
    result=dict(expected=modes, source=os.environ["PRODUCT_SHA"], results={}, failures={})
    with tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"]) as directory:
        root=Path(directory).resolve();release=root/"release"
        shutil.copytree(base/"release",release)
        binary=release/"runsc"
        result["runsc_sha256"]=hashlib.sha256(binary.read_bytes()).hexdigest()
        for m in modes:
            try:
                result["results"][m]=case(binary,base/"test/rbe/forward_gso/router",root/m,{"one":1,"two":2}[m])
            except Exception:
                result["failures"][m]=traceback.format_exc()
            (records/"verdict.json").write_text(json.dumps(result,indent=2))
    result["verified"]=set(result["results"])==set(modes) and not result["failures"]
    (records/"verdict.json").write_text(json.dumps(result,indent=2))
    suite=ET.Element("testsuite",name="LinkCapture",tests=str(len(modes)),failures=str(len(result["failures"])))
    for m in modes:
        node=ET.SubElement(suite,"testcase",classname="LinkCapture",name=m)
        if m in result["failures"]:ET.SubElement(node,"failure").text=result["failures"][m]
    ET.ElementTree(suite).write(os.environ["XML_OUTPUT_FILE"])
    print(json.dumps(result,indent=2))
    return 0 if result["verified"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
