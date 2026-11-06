"""Run both interfaces on the native runtime and inspect complete capture records."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import time

work=Path(os.environ["RUNNER_TEMP"])/"tcpdump-reserve"
image="gvisor.dev/images/basic/tcpdump:latest"
marker={b"test",b"testtest"}


def cmd(*args,seconds=30):
    return subprocess.run(list(args),capture_output=True,timeout=seconds)


def save(report):
    (work/"capture.json").write_text(json.dumps(report,indent=2)+"\n")


def packets(path):
    data=path.read_bytes()[:512*1024]
    if len(data)<24:
        return None,[]
    end={b"\xd4\xc3\xb2\xa1":"<", b"\xa1\xb2\xc3\xd4":">",b"\x4d\x3c\xb2\xa1":"<",b"\xa1\xb2\x3c\x4d":">"}.get(data[:4])
    if not end:
        return None,[]
    typ=struct.unpack_from(end+"I",data,20)[0]&0xffff
    if typ not in (1,276):
        return typ,[]
    offset=24
    frames=[]
    while len(data)-offset>=16 and len(frames)<64:
        n=struct.unpack_from(end+"I",data,offset+8)[0]
        if n>65536 or offset+16+n>len(data):
            break
        frame=data[offset+16:offset+16+n]
        offset+=16+n
        if typ==276:
            if len(frame)<20 or frame[:2]!=b"\x08\x00":
                continue
            index=int.from_bytes(frame[4:8],"big")
            ip=frame[20:]
        else:
            if len(frame)<14 or frame[12:14]!=b"\x08\x00":
                continue
            index=None
            ip=frame[14:]
        if len(ip)<28 or ip[0]>>4!=4 or ip[9]!=17 or ip[12:20]!=b"\x7f\x00\x00\x01"*2:
            continue
        pos=(ip[0]&15)*4
        if len(ip)<pos+8 or pos<20:
            continue
        src,dst,udp_len,_=struct.unpack_from("!HHHH",ip,pos)
        payload=ip[pos+8:pos+udp_len]
        if src==dst==9999 and udp_len==8+len(payload) and payload in marker:
            frames.append({"data":payload.decode(),"src_port":src,"dst_port":dst,"ifindex":index})
    return typ,frames


def run(runtime,iface,case):
    cid=None
    proc=None
    path=work/(runtime+"-"+iface+".pcap")
    log=work/(runtime+"-"+iface+".stderr")
    try:
        create=cmd("docker","create","--runtime="+runtime,"--cap-add=NET_RAW","--mount",
                   f"type=bind,src={shutil.which('busybox')},dst=/probe/busybox,readonly",
                   image,"tcpdump","-nn","-U","-s","128","-i",iface,"-w","-","udp port 9999")
        cid=create.stdout.decode().strip()
        if create.returncode or not re.fullmatch(r"[0-9a-f]{64}",cid):
            cid=None
            raise RuntimeError("create")
        actual=cmd("docker","inspect","--format","{{.HostConfig.Runtime}}",cid)
        case["selected_runtime"]=actual.stdout.decode().strip()==runtime and actual.returncode==0
        with path.open("wb") as pcap,log.open("wb") as err:
            proc=subprocess.Popen(["docker","start","--attach",cid],stdout=pcap,stderr=err)
            for _ in range(100):
                text=log.read_text(errors="replace")
                if f"listening on {iface}," in text or proc.poll() is not None:
                    break
                time.sleep(.25)
            case["listening"]=f"listening on {iface}," in text
            case["dlt_refused"]="LINUX_SLL2 is not one of the DLTs supported" in text
            if case["listening"]:
                uid=cmd("docker","exec",cid,"/probe/busybox","cat","/proc/1/status")
                case["uids_gids"]={key:[int(w) for w in val.split()] for key,val in re.findall(r"(?m)^(Uid|Gid):\s*([0-9 \t]+)$",uid.stdout.decode(errors="replace"))}
                case["sender_exit"]=cmd("docker","exec",cid,"tcpdump_sender").returncode
                for _ in range(60):
                    _,frames=packets(path)
                    if {row["data"].encode() for row in frames}==marker or proc.poll() is not None:
                        break
                    time.sleep(.1)
            if proc.poll() is None:
                cmd("docker","kill","--signal=INT",cid)
            try:
                case["capture_exit"]=proc.wait(timeout=12)
            except subprocess.TimeoutExpired:
                cmd("docker","kill",cid)
                case["capture_exit"]=proc.wait(timeout=10)
        typ,frames=packets(path)
        case.update(dlt=typ,frames=frames,pcap_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        case["verified"]=bool(case["selected_runtime"] and case["listening"] and case.get("sender_exit")==0 and case["capture_exit"]==0 and
            case.get("uids_gids")=={"Uid":[128]*4,"Gid":[128]*4} and typ==(276 if iface=="any" else 1) and
            {row["data"].encode() for row in frames}==marker and all(typ!=276 or row["ifindex"]>0 for row in frames))
    except (RuntimeError,ValueError,OSError,subprocess.TimeoutExpired) as exc:
        case["error"]=type(exc).__name__
    finally:
        if cid:
            cmd("docker","rm","-f",cid)
        if proc is not None and proc.poll() is None:
            proc.kill();proc.wait()


report={"source":"5de0b08c436460cd3f68cf9a3dfc0bef8f891407","native":platform.machine(),"cases":{}}
inspect=json.loads(cmd("docker","image","inspect",image).stdout)[0]
report.update(image_id=inspect["Id"],image_arch=inspect["Architecture"],declared_user=inspect["Config"]["User"])
for runtime in ("runc","packet-reserve"):
    for iface in ("any","lo"):
        case={"verified":False}
        report["cases"][runtime+"/"+iface]=case
        save(report)
        run(runtime,iface,case)
        save(report)
report["verified"]=all(c["verified"] for c in report["cases"].values())
save(report)
