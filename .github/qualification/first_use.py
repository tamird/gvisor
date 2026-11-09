"""Exercise first use with an explicitly negotiated MSS on both network protocols."""
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import time
import xml.etree.ElementTree as ET

import previous
import credit
import timer_teardown as recovery

WORK=Path(os.environ["RUNNER_TEMP"])/"tcp-experiment-first-use"
OUT=WORK/"results"
OWNER=previous.OWNER
F=previous.FILE
H=F.with_name("e2e.go")
BASE="9cbd7935e7b1e1f218d5d4e98a378b28695f842d"
FIX="7ba94b165d85a3fb49f0dcdd00f238cf76c751e1"
DONOR="fe8e3a2bd83cc3bee9995762569c7f40dfa13a8e"
CASE="TestTCPMigrationExperimentOption"
V4=credit.V4
V6=credit.V6
OLD=credit.OLD
NOGOS=["//pkg/tcpip:tcpip_nogo","//pkg/tcpip/stack:stack_nogo","//pkg/tcpip/transport/tcp:tcp_nogo"]
DISABLED="TestSetExperimentOptionWithOptionDisabled"
GSO=["TestSetExperimentOption","TestSetExperimentOptionIPv6"]
TABLE={CASE+"/"+family+"/"+src for family in ("IPv4","IPv6") for src in ("already_capable","newly_capable")}
report={"carrier":os.environ["GITHUB_SHA"],"conservative":BASE,"product":FIX,"prior_credit":13640749391,
        "verified":False,"cells":{},"public_inputs":recovery.seen}

def save():
    tmp=OUT/"result.tmp"
    tmp.write_text(json.dumps(report,indent=2,sort_keys=True)+"\n")
    tmp.replace(OUT/"result.json")

def git(*args):
    return subprocess.check_output(["git",*args])

def excerpt(source,start,end):
    assert source.count(start)==1 and source.count(end)==1 and source.index(start)<source.index(end)
    return source[source.index(start):source.index(end)]

def fixture(phase,source):
    end=b"// TestRestoreListenWithPreexistingConnection "
    table=excerpt(git("show",FIX+":"+str(F)),b"// TestTCPMigrationExperimentOption ",end)
    sign=b"defer view.Release()"
    assert table.count(sign)==1
    observed=table.replace(sign,sign+b'\n\t\t\t\t\tt.Logf("FIRST_USE_FRAME case=%s raw=%x", t.Name(), view.AsSlice())')
    if phase=="candidate":
        donor=git("show",DONOR+":"+str(F))
        v6=excerpt(donor,b"// TestIPv6MigrationPayloadSize ",end)
        assert V6.encode() not in source and source.count(table)==1
        return source.replace(table,observed)+b"\n"+v6
    assert CASE.encode() not in source and b'"gvisor.dev/gvisor/pkg/buffer"' not in source
    return source.replace(b'"gvisor.dev/gvisor/pkg/state"',b'"gvisor.dev/gvisor/pkg/buffer"\n\t"gvisor.dev/gvisor/pkg/state"')+b"\n"+observed

def wire(text,name):
    v6="/IPv6/" in name
    capable=name.endswith("/already_capable")
    cost=8 if v6 else 4
    raw_frames=re.findall(r"FIRST_USE_FRAME case="+re.escape(name)+r" raw=([0-9a-f]+)",text)
    assert len(raw_frames)==3,(name,len(raw_frames))
    result=[]
    for raw,tag,n,byte in zip(raw_frames,(False,capable,True),(1200,1200-cost if capable else 1200,1200-cost),(0x42,0x17,0xa7)):
        pkt=bytes.fromhex(raw)
        if v6:
            assert pkt[0]>>4==6 and len(pkt)==40+int.from_bytes(pkt[4:6],"big") and pkt[6]==(253 if tag else 6)
            if tag: assert pkt[40:48]==bytes.fromhex("0600006300000000")
            tcp=pkt[40+(cost if tag else 0):]
            addresses=pkt[8:40]
        else:
            assert pkt[0]>>4==4 and pkt[9]==6 and len(pkt)==int.from_bytes(pkt[2:4],"big")
            offset=4*(pkt[0]&15)
            assert offset==20+(cost if tag else 0)
            if tag: assert pkt[20:24]==bytes.fromhex("1e040063")
            tcp=pkt[offset:]
            addresses=pkt[12:20]
        sp,dp,seq,ack=struct.unpack_from("!HHII",tcp)
        hlen=4*(tcp[12]>>4)
        assert 20<=hlen<=len(tcp) and tcp[13]&16 and tcp[hlen:]==bytes([byte])*n
        if not result: start,flow=seq,(sp,dp,ack,addresses)
        assert (sp,dp,ack,addresses)==flow and seq==(start+sum(p["bytes"] for p in result))%2**32
        result.append({"bytes":n,"tagged":tag,"sequence":seq,"sha256":hashlib.sha256(pkt).hexdigest()})
    return result

def assess(row,docs,phase):
    text=docs["test.log"].decode().replace("\x16","")
    xml=ET.fromstring(docs["test.xml"])
    if phase=="nogo":
        parts=re.split(r"(?m)^(amd64|arm64)\.\.\.",text)
        assert len(parts)==5 and set(parts[1::2])=={"amd64","arm64"}
        assert all(s.strip().splitlines()==["PASS"] for s in parts[2::2]) and row["status"]=="PASSED"
        assert not any(c.tag in ("failure","error","skipped") for c in xml.iter())
        row["architectures"]=parts[1::2]
        return
    negative={c for c in TABLE if c.endswith("/already_capable")} if phase=="conservative" else set()
    expected=set(TABLE)
    if phase=="candidate":
        expected.update((V4,V6+"/"+OLD,DISABLED))
        expected.update(test+"/host_GSO="+value for test in GSO for value in ("true","false"))
    ends=re.findall(r"(?m)^\s*--- (PASS|FAIL|SKIP): (Test\S+) [(]",text)
    allowed={a for leaf in expected for a in ["/".join(leaf.split("/")[:i]) for i in range(1,len(leaf.split("/"))+1)]}
    assert all(name in allowed and out!="SKIP" for out,name in ends)
    cases=list(xml.iter("testcase"))
    assert all(c.get("name") in allowed and c.find("skipped") is None for c in cases)
    row["leaves"]={}
    for case in sorted(expected):
        outcome,=[out for out,name in ends if name==case]
        elem,=[c for c in cases if c.get("name")==case]
        bad=case in negative
        assert outcome==("FAIL" if bad else "PASS") and any(x.tag in ("failure","error") for x in elem)==bad,case
        row["leaves"][case]=outcome
    assert row["status"]==("FAILED" if negative else "PASSED")
    row["wire"]={case:wire(text,case) for case in sorted(TABLE-negative)}
    if negative:
        for mss in (1196,1192):
            assert text.count(f"never enabled: send MSS = {mss}, <nil>, want 1200")==1
    else:
        assert text.count("ACK of 1200-byte packet at MSS 1196: cwnd 10 -> 11")==1
        assert text.count("MSS_CREDIT sent_packets=1 sent_bytes=1200 restored_mss=1192 cwnd_before=10 cwnd_after=11")==1
        previous.RESTORES={OLD:[True]}
        row["ipv6_old_ack"]=previous.verify_frames(text,"candidate")[OLD]

def run(phase,head):
    git("switch","-q","--detach",head)
    assert git("rev-parse","HEAD").decode().strip()==head and not git("diff","HEAD","--name-only")
    scope=report["cells"][phase]={"head":head,"tests":{}}
    save()
    labels=NOGOS if phase=="nogo" else [OWNER]
    selector="^"+CASE+"$" if phase=="conservative" else "^("+"|".join([CASE,*GSO,V4,V6,DISABLED])+")$/^(IPv4|IPv6|host_GSO=(true|false)|"+OLD+")$"
    old={}
    try:
        if phase!="nogo":
            for p in (F,H):
                source=p.read_bytes()
                old[p]=source
                data=fixture(phase,source) if p==F else git("show",FIX+":"+str(H))
                p.write_bytes(data)
                scope[str(p)]=hashlib.sha256(data).hexdigest()
        bep=WORK/(phase+".bep")
        log=WORK/(phase+".console")
        args=[str(WORK/"bazel"),"--nosystem_rc","--nohome_rc","--host_jvm_args=-Xmx3g","test","--config=x86_64","-c","opt",
              "--jobs=4","--lockfile_mode=off","--remote_executor=","--remote_cache=","--bes_backend=","--keep_going",
              "--spawn_strategy=processwrapper-sandbox,local","--strategy=GoStdlib=processwrapper-sandbox",
              "--strategy=GoStdlibList=processwrapper-sandbox","--strategy=TestRunner=local","--nocache_test_results",
              "--flaky_test_attempts=1","--test_timeout=540","--test_sharding_strategy=disabled","--build_event_json_file="+str(bep)]
        if phase!="nogo":
            args+=["--test_arg=-test.v","--test_env=GO_TEST_WRAP_TESTV=1","--test_filter="+selector]
        for _ in range(12):
            bep.unlink(missing_ok=True)
            with log.open("wb") as output:
                try:
                    rc=subprocess.run(args+["--distdir="+str(p) for p in sorted(recovery.dirs)]+labels,stdout=output,
                                      stderr=subprocess.STDOUT,timeout=max(1,min(40*60,end_time-time.monotonic()))).returncode
                except subprocess.TimeoutExpired:
                    rc=124
            events=[json.loads(line) for line in bep.read_bytes().splitlines(keepends=True) if line.endswith(b"\n")] if bep.exists() else []
            results=[e for e in events if "testResult" in e]
            if results or rc==124 or not recovery.acquire(log.read_text(errors="replace")): break
        scope["exit"]=rc
        scope["diagnostics"]=[l[:400] for l in log.read_text(errors="replace").splitlines() if l.startswith("ERROR:") or re.search(r"[.]go:\d+:\d+:",l)][-14:]
        completed={(v["label"].removeprefix("@@"),v.get("configuration",{}).get("id")) for e in events if (v:=e["id"].get("targetCompleted",{}))}
        for event in results:
            label=event["id"]["testResult"]["label"].removeprefix("@@")
            assert label in labels and label not in scope["tests"]
            row=scope["tests"][label]={}
            try:
                # The previous BEP reader saves both original outputs and its
                # checkpoint in a separate directory for each arm and owner.
                credit.report=row
                credit.OWNER=label
                credit.OUT=OUT/phase/label.split(":")[-1]
                credit.OUT.mkdir(parents=True,exist_ok=True)
                docs=credit.original(event)
                assert (label,row["configuration"]) in completed
                assess(row,docs,phase)
                row["verified"]=True
            except Exception as exc:
                row["error"]=repr(exc)[:450]
            save()
        assert set(scope["tests"])==set(labels) and all(r.get("verified") for r in scope["tests"].values())
        assert rc==(3 if phase=="conservative" else 0)
        scope["verified"]=True
    finally:
        for p,data in old.items(): p.write_bytes(data)
        save()

if __name__=="__main__":
    assert git("rev-parse",report["carrier"]+"^").decode().strip()==FIX
    end_time=time.monotonic()+65*60
    recovery.known.update((r[2],r) for r in json.loads((WORK/"inputs.json").read_bytes()))
    recovery.assets.DIST.mkdir(parents=True,exist_ok=True)
    for phase,head in (("conservative",BASE),("candidate",FIX),("nogo",FIX)):
        try: run(phase,head)
        except Exception as exc: report["cells"].setdefault(phase,{})["error"]=repr(exc)[:450]
        save()
    report["verified"]=all(s.get("verified",False) for s in report["cells"].values())
    save()
    raise SystemExit(not report["verified"])
