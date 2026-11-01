"""Compare a routed TCP transfer with and without host segmentation metadata."""

import collections
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import xml.etree.ElementTree as ET


def configure(family):
    global SOURCE, SINK, NEAR, FAR, AF, ETH, FAMILY
    FAMILY = int(family)
    os.environ["ROUTER_FAMILY"] = str(FAMILY)
    if FAMILY == 6:
        SOURCE,SINK,NEAR,FAR="fd11:1::1","fd11:2::2","fd11:1::2","fd11:2::1"
        AF,ETH=socket.AF_INET6,0x86dd
    else:
        SOURCE,SINK,NEAR,FAR="192.0.2.1","198.51.100.2","192.0.2.2","198.51.100.1"
        AF,ETH=socket.AF_INET,0x0800


configure(os.environ.get("ROUTER_FAMILY","4"))
PORT = 14011
BLOCK = bytes(range(256)) * 256
LENGTH = 16 * len(BLOCK)
MTU = 1466
SCRIPT = Path(__file__).resolve()


def run(*args):
    completed = subprocess.run(args, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(f"{args}: {completed.returncode}: {completed.stderr}")
    return completed.stdout


def serve():
    with socket.socket(AF, socket.SOCK_STREAM) as server:
        server.bind((SOURCE, PORT))
        server.listen(1)
        print("ready", flush=True)
        conn, _ = server.accept()
        with conn:
            conn.settimeout(12)
            for _ in range(16):
                conn.sendall(BLOCK)


def receive():
    start = time.monotonic()
    count = 0
    digest = hashlib.sha256()
    with socket.create_connection((SOURCE, PORT), timeout=4) as conn:
        while count < LENGTH and (left := 8 - (time.monotonic() - start)) > 0:
            conn.settimeout(left)
            try:
                chunk = conn.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            count += len(chunk)
            digest.update(chunk)
    print(json.dumps(dict(size=count, seconds=time.monotonic() - start, sha256=digest.hexdigest())))


def folded(data):
    data += bytes(len(data)%2)
    total=sum(word[0] for word in struct.iter_unpack("!H",data))
    while total>>16:
        total=(total&65535)+(total>>16)
    return total


def capture(dev, output, stop):
    seen=collections.Counter()
    examples=[]
    a,b=(socket.inet_pton(AF,x) for x in (SOURCE,SINK))
    with socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(ETH)) as packet:
        packet.setsockopt(263,15,1)
        packet.bind((dev,0)); packet.settimeout(.1)
        print("ready",flush=True)
        while not stop.exists():
            try:
                frame=packet.recv(131072)
            except TimeoutError: continue
            if len(frame)<64:continue
            flags,gtype,hlen,mss,start,coff=struct.unpack_from("<BBHHHH",frame)
            ip=frame[24:]
            if FAMILY==4:
                if ip[9]!=6 or ip[12:16]!=a or ip[16:20]!=b:continue
                off=4*(ip[0]&15);size=int.from_bytes(ip[2:4],"big")
                if folded(ip[:off])!=65535:seen["badIPChecksum"]+=1
                pseudo=a+b+struct.pack("!BBH",0,6,size-off)
            else:
                if ip[6]!=6 or ip[8:24]!=a or ip[24:40]!=b:continue
                off=40;size=40+int.from_bytes(ip[4:6],"big")
                pseudo=a+b+struct.pack("!I3xB",size-off,6)
            tcp=ip[off:size]
            if len(tcp)<20 or int.from_bytes(tcp[:2],"big")!=PORT:continue
            th=4*(tcp[12]>>4)
            if len(tcp)<=th:continue
            seen["packets"]+=1; seen["largestIP"]=max(seen["largestIP"],size)
            if flags&1:
                seen["partial"]+=1
                if int.from_bytes(tcp[16:18],"big")!=folded(pseudo) or start!=14+off or coff!=16:seen["badPartialChecksum"]+=1
            else:
                seen["complete"]+=1
                if folded(pseudo+tcp)!=65535:seen["badTCPChecksum"]+=1
            if size>MTU:
                seen["overMTU"]+=1
                if gtype==(1 if FAMILY==4 else 4) and mss:
                    seen["marked"]+=1
                    if off+th+mss<=MTU:seen["fits"]+=1
                if len(examples)<3:examples.append([size,flags,gtype,mss,start,coff])
    output.write_text(json.dumps(dict(seen,examples=examples)))


def actual_df():
    # A single ordinary TCP packet bigger than the outgoing interface. Match
    # the returned ICMP error to our TCP ports and IPv4 identifier.
    a,b=(socket.inet_aton(x) for x in (SOURCE,SINK))
    body=b"D"*1500
    tcp=struct.pack("!HHIIBBHHH",40123,14012,123,0,5<<4,16,4096,0,0)
    xsum=65535-folded(a+b+struct.pack("!BBH",0,6,len(tcp)+len(body))+tcp+body)
    tcp=tcp[:16]+struct.pack("!H",xsum)+tcp[18:]
    ip=struct.pack("!BBHHHBBH4s4s",0x45,0,20+len(tcp)+len(body),0x6ace,0x4000,64,6,0,a,b)
    with socket.socket(socket.AF_INET,socket.SOCK_RAW,socket.IPPROTO_ICMP) as obs,socket.socket(socket.AF_INET,socket.SOCK_RAW,socket.IPPROTO_RAW) as raw:
        obs.settimeout(2);raw.setsockopt(socket.IPPROTO_IP,10,3)
        raw.sendto(ip+tcp+body,(SINK,0))
        while True:
            reply=obs.recv(65535);icmp=reply[4*(reply[0]&15):];inner=icmp[8:]
            if len(inner)>=28 and icmp[:2]==bytes([3,4]) and inner[4:6]==bytes.fromhex("6ace") and inner[12:16]==a and inner[16:20]==b and inner[20:24]==tcp[:4]:
                print(json.dumps({"sentIP":len(ip+tcp+body),"reportedMTU":int.from_bytes(icmp[6:8],"big")}));return


def unmount_null_netns(path):
    # runsc shares a pinned null gofer namespace below --root even after the
    # container is deleted. Detach it before removing this lab's runtime root.
    while path.exists() and subprocess.run(["mountpoint", "-q", path]).returncode == 0:
        run("umount", "-l", path)


def guest_link(work,host):
    stats=collections.Counter()
    for path in work.glob("debug-*.log"):
        for line in path.read_text(errors="replace").splitlines():
            for direction in ["recv","send"]:
                if f"{direction} tcp" in line and SOURCE in line and SINK in line and f":{PORT}" in line and " gso:" in line:
                    stats[direction+"GSO"]+=1
            if "send icmp" in line and ("destination unreachable" in line and "code:4" in line or "packet too big" in line):
                stats["tooBig"]+=1
    return dict(stats)


def spawn(scope, args, stderr=None):
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=stderr, text=True)
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


def compare(mode, root, runtime, helper):
    configure(mode[-1])
    work = root / mode
    work.mkdir()
    names = ["gso-source", "gso-router", "gso-sink"]
    src, router, sink = names
    with contextlib.ExitStack() as scope:
        for ns in names:
            run("ip", "netns", "add", ns)
            scope.callback(subprocess.run, ["ip", "netns", "del", ns], check=False)
            run("ip", "-n", ns, "link", "set", "lo", "up")
        for n1, dev1, addr1, n2, dev2, addr2 in [
            (src, "left", SOURCE, router, "rin", NEAR),
            (router, "rout", FAR, sink, "right", SINK),
        ]:
            run("ip", "link", "add", dev1, "netns", n1, "type", "veth", "peer", "name", dev2, "netns", n2)
            for ns, dev, addr in [(n1, dev1, addr1), (n2, dev2, addr2)]:
                run("ip", "-n", ns, "addr", "add", addr + ("/64" if FAMILY==6 else "/30"), "dev", dev, *(["nodad"] if FAMILY==6 else []))
                run("ip", "-n", ns, "link", "set", dev, "mtu", str(MTU), "up")
        run("ip", "-n", src, "route", "add", "default", "via", NEAR)
        run("ip", "-n", sink, "route", "add", "default", "via", FAR)
        features = {}
        if mode.startswith("segmented"):
            for ns, dev, wanted in ((src, "left", {"tso": "tcp-segmentation-offload", "gso": "generic-segmentation-offload"}),
                                    (router, "rin", {"gro": "generic-receive-offload"})):
                run("ip","netns","exec",ns,"ethtool","-K",dev,*(x for flag in wanted for x in (flag,"off")))
                reported = run("ip","netns","exec",ns,"ethtool","-k",dev)
                for name in wanted.values():
                    if not re.search("^"+name+r": off(?:\s|$)", reported,re.M):
                        raise RuntimeError(f"{dev} {name} was not disabled")
                    features[f"{dev}:{name}"] = "off"
        ingress = work / "ingress.json"
        stop = work / "stop"
        watcher = spawn(scope, ["ip", "netns", "exec", router, sys.executable, SCRIPT, "capture", "rin", ingress, stop])
        ready(watcher)
        if mode == "linux":
            run("ip", "netns", "exec", router, "sysctl", "-qw", "net.ipv4.ip_forward=1")
        else:
            fs = work / "rootfs"
            fs.mkdir()
            shutil.copy2(helper, fs / "init")
            (fs / "proc").mkdir()
            config = dict(ociVersion="1.0.2", root=dict(path="rootfs", readonly=True),
                          process=dict(args=["/init"], cwd="/", user=dict(uid=0, gid=0),
                                       capabilities={k: ["CAP_NET_ADMIN"] for k in ("effective", "permitted", "bounding")}),
                          mounts=[dict(destination="/proc", type="proc", source="proc")],
                          linux=dict(namespaces=[dict(type=k) for k in ("pid", "ipc", "uts", "mount")] +
                                     [dict(type="network", path="/var/run/netns/" + router)]))
            (work / "config.json").write_text(json.dumps(config))
            base = [runtime, "--root=" + str(work / "state"), "--ignore-cgroups=true", "--platform=systrap",
                    "--debug=true", "--debug-log=" + str(work / "debug-%COMMAND%.log"), "--log-packets=true",
                    "--gso=" + ("false" if mode == "gvisor-no-host-gso" else "true")]
            scope.callback(unmount_null_netns, work / "state" / "null-netns")
            scope.callback(subprocess.run, [*base, "delete", "--force", "router"], check=False)
            errors = scope.enter_context((work / "runtime.stderr").open("w"))
            ready(spawn(scope, [*base, "run", "--bundle", work, "router"], stderr=errors))
        # The router's Sentry still submits virtio TX metadata. Disable host
        # offload after runsc has opened its sockets to inspect final segments
        # and the checksums Linux emitted on the veth peer.
        run("ip","netns","exec",router,"ethtool","-K","rout","tso","off","gso","off","tx","off")
        run("ip","netns","exec",sink,"ethtool","-K","right","gro","off")
        egress=work/"egress.json"
        observer=spawn(scope,["ip","netns","exec",sink,sys.executable,SCRIPT,"capture","right",egress,stop])
        ready(observer)
        ready(spawn(scope, ["ip","netns","exec",src,sys.executable,SCRIPT,"serve"]))
        try:
            result=json.loads(run("ip","netns","exec",sink,sys.executable,SCRIPT,"receive"))
        finally:
            stop.touch()
            if watcher.wait(timeout=3) or observer.wait(timeout=3):raise RuntimeError("wire observation failed")
        result["ingress"]=json.loads(ingress.read_text())
        result["egress"]=json.loads(egress.read_text())
        # Count any spurious PTB in the flow BEFORE deliberately eliciting one.
        result["guestLink"]=guest_link(work,{})
        result["features"]=features
        if mode=="aggregate4":
            for ns,dev in [(src,"left"),(router,"rin")]:run("ip","-n",ns,"link","set",dev,"mtu","1800")
            result["genuineDF"]=json.loads(run("ip","netns","exec",src,sys.executable,SCRIPT,"df"))
        return result


def main(modes: list[str]) -> int:
    records = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    runfiles = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"]
    result = dict(source=os.environ["PRODUCT_SHA"], expected=modes, results={}, failures={})
    with tempfile.TemporaryDirectory(dir=os.environ["TEST_TMPDIR"]) as directory:
        root = Path(directory).resolve()
        release = root / "release"
        runtime = release / "runsc"
        if modes != ["linux"]:
            shutil.copytree(runfiles / "release", release)
            result["runsc_sha256"] = hashlib.sha256(runtime.read_bytes()).hexdigest()
        for mode in modes:
            try:
                result["results"][mode] = compare(mode, root, runtime, runfiles / "test/rbe/forward_gso/router")
            except Exception:
                result["failures"][mode] = traceback.format_exc()
                work = root / mode
                partial = result.setdefault("partial", {}).setdefault(mode, {})
                if (host := work / "ingress.json").is_file():
                    partial["ingress"] = json.loads(host.read_text())
                partial["guestLink"] = guest_link(work, partial.get("ingress", {}))
                if "ingress" in partial:
                    partial["ingress"].pop("dfGsoPackets", None)
                    partial["ingress"].pop("packetLengths", None)
                if (stderr := work / "runtime.stderr").is_file():
                    partial["runtimeError"] = [line[:1200] for line in stderr.read_text(errors="replace").splitlines()
                        if not re.search(r"authorization|api.key|password|bearer|remote.header|token",line,re.I)][-5:]
            (records / "verdict.json").write_text(json.dumps(result, indent=2))
    expected_hash = hashlib.sha256(BLOCK * 16).hexdigest()
    for mode, row in result["results"].items():
        row["complete"] = row["size"] == LENGTH and row["sha256"] == expected_hash
        inp,out=row["ingress"],row["egress"]
        if not row["complete"] or row["guestLink"].get("tooBig"):result["failures"][mode]="incomplete transfer or unexpected ICMP"
        elif mode.startswith("aggregate") and not (inp.get("overMTU",0)>0 and inp["overMTU"]==inp.get("fits",0) and row["guestLink"].get("sendGSO")):result["failures"][mode]="no actual forwarded host TCP aggregates"
        elif mode.startswith("segmented") and (inp.get("overMTU") or not inp.get("packets")):result["failures"][mode]="segmented control missing"
        elif out.get("packets",0)<700 or out.get("overMTU") or out.get("complete",0)!=out["packets"] or out.get("badIPChecksum") or out.get("badTCPChecksum"):result["failures"][mode]="final TCP segment/checksum mismatch"
        elif mode=="aggregate4" and row.get("genuineDF")!={"sentIP":1540,"reportedMTU":MTU}:result["failures"][mode]="ordinary DF packet not rejected at actual output MTU"
    result["verified"] = not result["failures"] and len(result["results"]) == len(modes)
    (records / "verdict.json").write_text(json.dumps(result, indent=2))
    suite = ET.Element("testsuite", name="ForwardedGSO", tests=str(len(modes)), failures=str(len(result["failures"])))
    for mode in modes:
        case = ET.SubElement(suite, "testcase", classname="ForwardedGSO", name=mode)
        if mode in result["failures"]:
            ET.SubElement(case, "failure").text = result["failures"][mode]
    ET.ElementTree(suite).write(os.environ["XML_OUTPUT_FILE"])
    print(json.dumps(result, indent=2))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["serve"]:
            serve()
        case ["receive"]:
            receive()
        case ["capture",dev,output,stop]:
            capture(dev,Path(output),Path(stop))
        case ["df"]:
            actual_df()
        case ["aggregate4","segmented4","aggregate6","segmented6"]:
            sys.exit(main(sys.argv[1:]))
        case other:
            raise ValueError(other)
