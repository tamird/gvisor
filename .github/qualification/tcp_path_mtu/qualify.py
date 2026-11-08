"""Read the genuine TestRunner, private peer frames and bounded original diagnostics."""
import json
import os
from pathlib import Path
import re
import struct
import subprocess
from urllib.parse import urlsplit, unquote
import xml.etree.ElementTree as ET
from decode import sha,decode

W=Path(os.environ.get("RUNNER_TEMP","/tmp"))/"mtu-wire"
OUT=W/"out"
OWNER="//test/tcp_path_mtu_qualification:wire_test"
SOURCE="27e1cb29525c5c182066600391bc150a9aa14421"
REPORT={"source":SOURCE,"carrier":os.environ.get("GITHUB_SHA"),"verified":False,"cases":{}}


def save(): (OUT/"result.json").write_text(json.dumps(REPORT,indent=2,sort_keys=True)+"\n")


def syscall_headers():
    # Preserve the first iovec's length and the actual 10-byte virtio header.
    lines=[]
    for f in W.glob("syscall.*"):
        for s in f.read_text(errors="replace").splitlines():
            m=re.match(r"^(\d+\.\d+) (sendmmsg|writev)\((\d+),",s)
            if not m:continue
            first=re.search(r'iov_base="((?:\\x[0-9a-f]{2})+)"(?:\.\.\.)?, iov_len=(\d+)',s)
            if not first:continue
            item={"us":round(float(m[1])*1e6),"call":m[2],"fd":int(m[3]),"first_iov":int(first[2])}
            if item["first_iov"]==10:
                h=bytes.fromhex(first[1].replace("\\x",""))
                if len(h)==10:
                    flags,typ,hlen,gsize,cstart,coff=struct.unpack("<BBHHHH",h)
                    item["virtio"]=dict(flags=flags,gso_type=typ,hdr_len=hlen,gso_size=gsize,csum_start=cstart,csum_offset=coff)
            lines.append(item)
    return lines


def validate_row(r,index,syscalls):
    if r["Outcome"]=="setup_failure":
        assert r["StageError"]
        return
    assert r["Connected"] and r["Accepted"]
    family,mode,mss,phase=r["Family"],r["Mode"],r["PeerMSS"],r["Phase"]
    assert r["SelectedGSO"]=={"none":0,"software":2,"host":1}[mode] and r["Vnet"]==int(mode=="host")
    syns=[p["SynMSS"] for call in r["PeerHandshakeCalls"] or [] for p in call["Packets"] or [] if p["Flags"]&0x12==0x12]
    assert syns and set(syns)=={mss or (1460 if family=="v4" else 1440)}
    r["negotiated_peer_mss"]=syns[0]
    marker=b"gvisor-toggle-witness" if phase=="off_again" else b""
    assert r["PrimeWrote"]==len(marker) and r["PrimeRead"]<=len(marker)
    body=bytes((i*29+i//251+index)&255 for i in range(7*1440+73))
    want=marker[r["PrimeRead"]:] + body
    assert r["Expected"]==sha(want) and r["Digest"]==sha(want[:r["Read"]])
    assert r["Wrote"]==7*1440+73
    assert (r["Outcome"]=="delivered")==(len(want)==r["Read"])
    assert r["Outcome"] in ("delivered","undelivered") and (r["Read"]==len(want) or r["StageError"])
    for key in ("Handshake","PrimeFrames","Frames"):
        r[key+"_summary"]=decode(r.get(key),r)
        r.pop(key,None)
    first, = {((p["Seq"]+1)&0xffffffff) for c in r["HandshakeCalls"] or [] for p in c["Packets"] or [] if p["Flags"]&2}
    stream=marker+body
    covered=bytearray(len(stream))
    for key in ("PrimeFrames_summary","Frames_summary"):
        summary=r[key]
        for entry in summary["frames"] + summary["reassembled"]:
            p=entry.get("tcp")
            if not p or not p["data"]:continue
            off=(p["seq"]-first)&0xffffffff
            stop=off+p["data"]
            p["stream_offset"]=off
            p["payload_matches"]=stop<=len(stream) and sha(stream[off:stop])==p["payload_sha256"]
            if p["payload_matches"]:covered[off:stop]=bytes([1])*p["data"]
    r["matched_peer_bytes"]=sum(covered)
    r["stream_bytes"]=len(stream)
    if r["Outcome"]=="delivered":
        assert r["matched_peer_bytes"]==len(stream)
    for key in ("PrimeCalls","Calls"):
        for call in r[key] or []:
            call["syscalls"]=[entry for entry in syscalls if entry["fd"]==r["SourceFD"] and call["BeginUS"]<=entry["us"]<=call["EndUS"]]


def main():
    assert REPORT["carrier"]
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==SOURCE
    for f in ("BUILD","wire_test.go"):
        assert (Path("test/tcp_path_mtu_qualification")/f).read_bytes()==(W/f).read_bytes()
    want={f"{mode}/{family}/mss_{mss}/{phase}":i for i,(mode,family,mss,phase) in enumerate(
        (m,f,s,p) for m in ("none","software","host") for f in ("v4","v6") for s in (0,1200) for p in ("off","on","off_again")) if mode=="host"}
    bep=W/"bep"
    with (W/"bazel.log").open("w") as f:
        rc=subprocess.run([W/"bazel","--nosystem_rc","--nohome_rc","--host_jvm_args=-Xmx4g","test","--config=x86_64","-c","opt","--jobs=4","--lockfile_mode=off","--remote_executor=","--remote_cache=","--bes_backend=","--strategy=TestRunner=local","--nocache_test_results","--test_sharding_strategy=disabled","--flaky_test_attempts=1","--test_timeout=360","--test_tag_filters=","--build_tag_filters=","--test_env=GO_TEST_WRAP_TESTV=1","--test_env=MTU_WORK="+str(W),"--test_arg=-test.v","--test_arg=-test.run=^TestExperimentMTUWire$/^host$","--run_under="+str(W/"netns.sh"),"--build_event_json_file="+str(bep),OWNER],stdout=f,stderr=subprocess.STDOUT).returncode
    REPORT["exit"]=rc
    if rc:REPORT["diagnostic"]=[s[:420] for s in (W/"bazel.log").read_text(errors="replace").splitlines() if "ERROR:" in s or ".go:" in s or "FAIL:" in s][:16]
    for k in ("outer-uid","uid_map","initial-links","links","mtua.features","mtub.features","offload-setup"):
        p=W/k
        if p.is_file() and p.stat().st_size<1<<16:REPORT[k]=p.read_text().splitlines()
    save()
    events=[json.loads(s) for s in bep.read_text().splitlines()]
    ev,=[e for e in events if "testResult" in e];res=ev["testResult"];ident=ev["id"]["testResult"]
    assert ident["label"].removeprefix("@@")==OWNER
    REPORT["test"]=dict(strategy=res.get("executionInfo",{}).get("strategy"),status=res["status"],cached=bool(res.get("cachedLocally") or res.get("executionInfo",{}).get("cachedRemotely")),configuration=ident["configuration"]["id"])
    assert all(int(ident.get(k,1))==1 for k in ("shard","run","attempt"))
    for name in ("test.log","test.xml"):
        f,=[a for a in res["testActionOutput"] if a["name"]==name];u=urlsplit(f["uri"])
        p=Path(unquote(u.path));assert u.scheme=="file" and not (u.netloc or u.query or u.fragment)
        assert p.samefile(Path("bazel-testlogs")/OWNER[2:].replace(":","/")/name) and p.stat().st_size < 20<<20
        data=p.read_bytes();assert not f.get("digest") or sha(data)==f["digest"]
        assert len(data)==int(f.get("length",len(data)))
        (OUT/name).write_bytes(data);REPORT["test"][name]=sha(data)
    log=(OUT/"test.log").read_text(errors="replace").replace("\x16","")
    syscalls=syscall_headers();REPORT["syscall_count"]=len(syscalls)
    for compact in re.findall(r'(?m)MTU_RESULT (\{.*\})$',log):
        r=json.loads(compact);k=f'{r["Mode"]}/{r["Family"]}/mss_{r["PeerMSS"]}/{r["Phase"]}'
        assert k in want and k not in REPORT["cases"]
        REPORT["cases"][k]=r
        try:
            validate_row(r,want[k],syscalls)
            r["verified"]=True
        except Exception as err:r["observer_error"]=f"{type(err).__name__}: {err}"
        save()
    assert set(REPORT["cases"])==set(want)
    root=ET.fromstring((OUT/"test.xml").read_bytes())
    assert root.find(".//failure") is None and root.find(".//error") is None
    assert all(len(re.findall(r"(?m)^\s*--- PASS: TestExperimentMTUWire/"+re.escape(k)+r" \(",log))==1 for k in want)
    assert rc==0 and REPORT["test"]["strategy"] in ("local","standalone") and not REPORT["test"]["cached"] and res["status"]=="PASSED"
    assert [e["finished"]["exitCode"].get("code",0) for e in events if "finished" in e]==[0]
    assert all(r.get("verified") for r in REPORT["cases"].values())
    original=(W/"stock.json").read_bytes()
    assert sha(original)=="fdb717e99476888d9882fe1a77cda5f8dcabb9345ddb073a1e69567df0f77977"
    stock=json.loads(original)
    assert stock["artifact_sha256"]=="6101d0ab3aabe539ebceb2bd2d2c8e7687b0ce1901b8c98fa4e8bcfe174bb478"
    REPORT["stock_artifact"]=stock["url"]
    for k,r in REPORT["cases"].items():
        r["stock"]={key:stock["cases"][k][key] for key in ("Outcome","Read","PrimeRead","matched_peer_bytes")}
    save()
    for k,r in REPORT["cases"].items():
        assert r["Outcome"]!="setup_failure"
        earlier=r["stock"]
        if r["Phase"] in ("off","off_again"):
            assert r["Outcome"]==earlier["Outcome"]=="delivered"
        if r["Phase"]=="off_again":
            assert earlier["PrimeRead"]==0
            r["option_on_marker_delivered"]=(r["PrimeRead"]==21)
        groups={}
        for call in (r["Calls"] or [])+(r["PrimeCalls"] or []):
            netlens={p["NetHeader"] for p in call["Packets"] or [] if p["GSO"]["NeedsCsum"]}
            for p in call["Packets"] or []:
                assert not p["GSO"]["NeedsCsum"] or p["GSO"]["L3HdrLen"]==p["NetHeader"]
            if len(netlens)!=1:continue
            length,=netlens
            for syscall in call["syscalls"]:
                v=syscall.get("virtio")
                if v and v["flags"]&1:
                    assert v["csum_start"]==14+length and v["csum_offset"]==16
                    groups[length]=groups.get(length,0)+1
        r["joined_checksum_starts"]=groups
        base=20 if r["Family"]=="v4" else 40
        ext=4 if r["Family"]=="v4" else 8
        assert (base+ext if r["Phase"]!="off" else base) in groups
        save()
    REPORT["verified"]=True;save()


if __name__=="__main__":
    try:main()
    except Exception as err:
        import traceback
        REPORT["observer_error"]=f"{type(err).__name__}: {err}"
        REPORT["observer_line"]=traceback.extract_tb(err.__traceback__)[-1].lineno
        save();raise
