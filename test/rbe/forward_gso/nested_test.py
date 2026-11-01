"""Forward from a host GSO-capable link onto a bridge inside the router."""
import contextlib
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import traceback
import xml.etree.ElementTree as ET

import forwarding_test as lab

MODES = ["guest-aggregate"]


def measure(mode, root, runtime, helper):
    work = root / mode
    work.mkdir()
    result = {}
    src, outer = "bridge-source", "bridge-router"
    stop = work / "stop"
    capture = work / "ingress.json"
    with contextlib.ExitStack() as scope:
        for ns in (src, outer):
            lab.run("ip", "netns", "add", ns)
            scope.callback(subprocess.run, ["ip", "netns", "del", ns], check=False)
            lab.run("ip", "-n", ns, "link", "set", "lo", "up")
        lab.run("ip","link","add","left","netns",src,"type","veth","peer","name","rin","netns",outer)
        for ns, device, addr in ((src,"left",lab.SOURCE),(outer,"rin",lab.NEAR)):
            lab.run("ip","-n",ns,"addr","add",addr+"/30","dev",device)
            lab.run("ip","-n",ns,"link","set",device,"mtu",str(lab.MTU),"up")
        lab.run("ip","-n",src,"route","add","default","via",lab.NEAR)
        if mode.endswith("segmented"):
            lab.run("ip","netns","exec",src,"ethtool","-K","left","tso","off","gso","off")
            lab.run("ip","netns","exec",outer,"ethtool","-K","rin","gro","off")
        observer = lab.spawn(scope, ["ip","netns","exec",outer,sys.executable,lab.SCRIPT,"capture","rin",capture,stop])
        lab.ready(observer)
        lab.ready(lab.spawn(scope, ["ip","netns","exec",src,sys.executable,lab.SCRIPT,"serve"]))
        if mode.startswith("linux"):
            cmd = ["ip","netns","exec",outer,helper]
        else:
            fs = work/"rootfs"
            fs.mkdir()
            shutil.copy2(helper, fs/"init")
            (fs/"proc").mkdir()
            caps = ["CAP_NET_ADMIN","CAP_SYS_ADMIN","CAP_NET_RAW"]
            cfg = dict(ociVersion="1.0.2", root=dict(path="rootfs",readonly=True),
                process=dict(args=["/init"],cwd="/",user=dict(uid=0,gid=0),
                    capabilities={k:caps for k in ("effective","permitted","bounding")}),
                mounts=[dict(destination="/proc",type="proc",source="proc")],
                linux=dict(namespaces=[dict(type=k) for k in ("pid","ipc","uts","mount")]+
                    [dict(type="network",path="/var/run/netns/"+outer)]))
            (work/"config.json").write_text(json.dumps(cfg))
            base=[runtime,"--root="+str(work/"state"),"--ignore-cgroups=true","--platform=systrap",
                  "--debug=true","--debug-log="+str(work/"debug-%COMMAND%.log"),"--log-packets=true","--net-raw=true","--gso=true"]
            scope.callback(lab.unmount_null_netns, work/"state"/"null-netns")
            scope.callback(subprocess.run,[*base,"delete","--force","router"],check=False)
            cmd=[*base,"run","--bundle",work,"router"]
        try:
            proc=subprocess.run(cmd,capture_output=True,text=True,timeout=40)
            result["exit"]=proc.returncode
            result["setup"]=[line for line in proc.stdout.splitlines() if line.startswith("bridge-ready")]
            result["diagnostics"]=[line[:400] for line in proc.stderr.splitlines()+proc.stdout.splitlines() if re.search(r"(panic:|error|failed|cannot|bridge.go:[0-9]+)",line,re.I) and not re.search(r"(token|authorization|bearer|api.key|remote.header|password)",line,re.I)][:18]
            for line in proc.stdout.splitlines():
                if line.startswith("{"):
                    result["inner"]=json.loads(line)
        except subprocess.TimeoutExpired:
            result["timeout"]=True
        finally:
            stop.touch()
            if observer.wait(timeout=4):raise RuntimeError("ingress capture failed")
            result["ingress"]=json.loads(capture.read_text())
            result["guestLink"]=lab.guest_link(work,{})
    return result


def main():
    dest=Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    rf=Path(os.environ["TEST_SRCDIR"])/os.environ["TEST_WORKSPACE"]
    result=dict(source=os.environ["PRODUCT_SHA"],expected=MODES,results={},failures={})
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        release=root/"release"
        shutil.copytree(rf/"release",release)
        helpers=[rf/"test/rbe/forward_gso"/path for path in ("bridge_/bridge","bridge")]
        helper=next(p for p in helpers if p.is_file())
        expected=hashlib.sha256(lab.BLOCK*16).hexdigest()
        for mode in MODES:
            try:
                row=measure(mode,root,release/"runsc",helper)
                result["results"][mode]=row
                inner=row.get("inner",{})
                assert row.get("exit")==0 and row["setup"] and inner.get("inner")=="cin" and inner["mtu"]==lab.MTU, "inner bridge or child failed"
                ing=row["ingress"]
                assert ing.get("captureDrops")==0, "capture incomplete"
                complete=inner.get("size")==lab.LENGTH and inner.get("sha256")==expected
                row["complete"]=complete
                if mode.endswith("aggregate"):
                    assert ing.get("overMTU",0)>0 and ing["overMTU"]==ing.get("fits"), "no confirmed host aggregates"
                else:
                    assert ing.get("overMTU",0)==0, "sender still aggregated"
                if mode!="guest-aggregate":
                    assert complete and ing.get("uniqueDataBytes")==lab.LENGTH and not row["guestLink"].get("tooBig"), "native/segmented transfer failed"
                else:
                    assert complete and ing.get("uniqueDataBytes")==lab.LENGTH and not row["guestLink"].get("tooBig"), "software bridge transfer incomplete or sent spurious PTB"
                    packet=inner["capture"]
                    assert packet["Bytes"]==lab.LENGTH and 0<packet["LargestIP"]<=lab.MTU and not any(packet[k] for k in ("BadIPChecksum","BadTCPChecksum","Error")), "inner peer missed segmented/checksummed data"
            except Exception:
                result["failures"][mode]=traceback.format_exc()
            (dest/"verdict.json").write_text(json.dumps(result,indent=2))
    result["verified"]=not result["failures"] and len(result["results"])==len(MODES)
    (dest/"verdict.json").write_text(json.dumps(result,indent=2))
    suite=ET.Element("testsuite",name="NestedBridge",tests=str(len(MODES)),failures=str(len(result["failures"])))
    for mode in MODES:
        case=ET.SubElement(suite,"testcase",classname="NestedBridge",name=mode)
        if mode in result["failures"]:ET.SubElement(case,"failure").text=result["failures"][mode]
    ET.ElementTree(suite).write(os.environ["XML_OUTPUT_FILE"])
    print(json.dumps(result,indent=2))
    return int(not result["verified"])


if __name__=="__main__":
    sys.exit(main())
