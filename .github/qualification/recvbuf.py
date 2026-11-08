"""Bind original and matched recv-buffer schedules to each DUT's packet trace."""
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import struct
import subprocess
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
import zipfile

def need(value, reason):
    if not value:
        raise ValueError(reason)


def sacks(opts):
    blocks = []
    while opts:
        kind = opts[0]
        if kind == 0:
            break
        if kind == 1:
            opts = opts[1:]
            continue
        need(len(opts) >= 2 and 2 <= opts[1] <= len(opts), "invalid TCP option")
        size = opts[1]
        if kind == 5:
            need(size >= 10 and (size - 2) % 8 == 0, "invalid SACK option")
            blocks.extend(struct.iter_unpack("!II", opts[2:size]))
        opts = opts[size:]
    return blocks


def parse_pcap(data):
    need(len(data) >= 24 and len(data) < 8 << 20, "pcap size")
    endians = {
        b"\xd4\xc3\xb2\xa1": "<",
        b"\xa1\xb2\xc3\xd4": ">",
        b"\x4d\x3c\xb2\xa1": "<",
        b"\xa1\xb2\x3c\x4d": ">",
    }
    endian = endians[data[:4]]
    need(struct.unpack_from(endian + "I", data, 20)[0] == 1, "expected Ethernet pcap")
    frames = []
    pos = 24
    while pos < len(data):
        need(pos + 16 <= len(data), "truncated record")
        sec, subsec, size, original = struct.unpack_from(endian + "IIII", data, pos)
        pos += 16
        need(size <= original and pos + size <= len(data), "truncated frame")
        fr = data[pos : pos + size]
        pos += size
        if len(fr) < 34 or fr[12:14] != b"\x08\x00" or fr[23] != 6:
            continue
        iplen = struct.unpack_from("!H", fr, 16)[0]
        iph = (fr[14] & 15) * 4
        start = 14 + iph
        need(
            iph >= 20 and len(fr) >= 14 + iplen and iplen >= iph + 20,
            "short IPv4 TCP frame",
        )
        tcp = fr[start : 14 + iplen]
        off = (tcp[12] >> 4) * 4
        need(20 <= off <= len(tcp), "invalid TCP data offset")
        sport, dport, seq, ack = struct.unpack_from("!HHII", tcp)
        frames.append(
            dict(
                route=(fr[26:30], fr[30:34]),
                sport=sport,
                dport=dport,
                seq=seq,
                ack=ack,
                flags=tcp[13],
                opts=tcp[20:off],
                sacks=sacks(tcp[20:off]),
                payload=tcp[off:],
                timestamp=[sec, subsec],
            )
        )
    return frames


OUT = Path(os.environ["RUNNER_TEMP"]) / "recvbuf"
MODES = ["original_burst", "held_prefix", "drained_prefix", "shrink_after_prefix", "large_buffer"]
TARGETS = ["//test/packetimpact/tests:tcp_rcv_buf_space_" + dut + "_test" for dut in ("native", "netstack")]
MASK = (1 << 32) - 1


def save(report):
    (OUT / "result.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def check_flow(frames, ports, base):
    out = [f for f in frames if (f["sport"], f["dport"]) == ports]
    peer = [f for f in frames if (f["dport"], f["sport"]) == ports]
    seq = next(f["seq"] for f in out if f["flags"] & 2 and not f["flags"] & 16)
    assert (seq + 1) & MASK == base
    tagged = [i for i,f in enumerate(frames) if (f["sport"],f["dport"]) == ports and f["flags"] & 1
              and not f["payload"] and f["seq"] == (base+1) & MASK]
    dsack = [(i,(f["ack"]-base)&MASK) for i,f in enumerate(frames) if (f["dport"],f["sport"]) == ports
             and f["flags"] & 16 and f["sacks"][:1] == [((base+1)&MASK,(base+2)&MASK)]]
    data = [(i,(f["seq"]-base)&MASK,len(f["payload"])) for i,f in enumerate(frames)
            if (f["sport"],f["dport"]) == ports and f["payload"]]
    return {"segments": [[s,n] for _,s,n in data],"tag_count":len(tagged),"dsack_ack":[a for _,a in dsack],
            "ordering": 1<=len(tagged)<=2 and len(dsack)>0 and data and max(i for i,_,_ in data)<tagged[0]<dsack[0][0],
            "ordinary_ack_progress": [(f["ack"]-base)&MASK for f in peer if f["flags"]==16][:80]}


def collect(row, event, typ):
    result = event["testResult"]
    row.update(status=result["status"], strategy=result.get("executionInfo",{}).get("strategy"),
               cached=bool(result.get("cachedLocally") or result.get("executionInfo",{}).get("cachedRemotely")),files={})
    docs = {}
    for record in result["testActionOutput"]:
        name=record["name"]
        if name not in ("test.log","test.xml","test.outputs__outputs.zip"):
            continue
        url=urlsplit(record["uri"])
        assert url.scheme=="file" and not url.netloc
        data=Path(unquote(url.path)).read_bytes()
        h=hashlib.sha256(data).hexdigest()
        assert len(data)<=32<<20 and (not record.get("digest") or record["digest"]==h)
        if "length" in record: assert int(record["length"])==len(data)
        row["files"][name]=[h,len(data)]
        docs[name]=data
    log=docs["test.log"].decode(errors="replace")
    row["go_cases"] = re.findall(r"--- (PASS|FAIL|SKIP): ([^ \r\n]+) \(",log)
    row["go_diagnostics"] = [s.strip()[:440] for s in log.splitlines() if
                              "tcp_rcv_buf_space_test.go:" in s or "tcp_rcv_buf_boundary_test.go:" in s][:160]
    row["xml_case_count"] = sum(1 for _ in ET.fromstring(docs["test.xml"]).iter("testcase"))
    assert not row["cached"] and row["strategy"] in ("local","standalone") and row["xml_case_count"]>0
    z=zipfile.ZipFile(io.BytesIO(docs["test.outputs__outputs.zip"]))
    names=[n for n in z.namelist() if n.endswith(".pcap")]
    assert len(names)==1,names
    raw=z.read(names[0])
    row["pcap_sha256"]=hashlib.sha256(raw).hexdigest()
    frames=parse_pcap(raw)
    if typ == "original":
        syns=[f for f in frames if f["flags"]==2]
        assert len(syns)==1
        f=syns[0]
        trace=check_flow(frames,(f["sport"],f["dport"]),(f["seq"]+1)&MASK)
        row["trace"]=trace
        assert sum(n for _,n in trace["segments"])==8193 and trace["tag_count"]==0
        assert len(row["go_cases"]) == 1 and row["go_cases"][0][1]=="TestReduceRecvBuf"
        # A genuine reproduction is a valid observed control.
        row["observed_failure"] = "Read total of 8193 bytes, same as sampleData length 8193" in log
        assert result["status"]=="PASSED" or row["observed_failure"],row["go_diagnostics"]
        return
    ids=re.findall(r"FLOW case=TestAdmissionBoundary/(\d+)/(\w+) sport=(\d+) dport=(\d+) base=(\d+)",log)
    summaries=re.findall(r"ADMISSION mode=(\w+) buf=(\d+) prefix=(\d+) early=(\d+) prior_window=(\d+) sent=(\d+) acked=(\d+) read=(\d+) tag=1-2 eagain=1",log)
    observed=row["observations"]={}
    # Standard go test output groups each leaf's diagnostics before its next leaf.
    for rnd, mode, sport,dport,base in ids:
        key=rnd+"/"+mode
        assert key not in observed and mode in MODES and int(rnd) in range(4)
        observed[key]=check_flow(frames,(int(sport),int(dport)),int(base))
    assert len(summaries)==len(ids)==20
    for (rnd,mode,*_),r in zip(ids,summaries):
        assert r[0]==mode
        val = observed[rnd+"/"+mode]
        val.update(zip(("buf","prefix","early","prior_window","sent","acked","read"),map(int,r[1:])))
        assert val["dsack_ack"] and all(a==val["acked"] for a in val["dsack_ack"]) and val["acked"]==val["read"] and val["ordering"]
        offset=0
        for seq,count in val["segments"]:
            assert seq==offset and 0<count<=1400
            offset+=count
        assert offset==val["sent"]
        if mode=="drained_prefix": assert val["early"]==4200
        if mode in ("shrink_after_prefix","large_buffer"): assert val["prior_window"]>=1400
    assert all(status=="PASS" for status,_ in row["go_cases"]) and result["status"]=="PASSED"


def main():
    report={"verified":False,"source":"451b92800b9cb82262dc8a735388f7cf4d17d895","kernel":platform.release(),"arms":{}}
    assert platform.machine()=="x86_64"
    for typ,case in (("original","TestReduceRecvBuf"),("controlled","TestAdmissionBoundary")):
        bep=OUT/(typ+".jsonl")
        cmd=[str(OUT.parent/"bazel"),"--nosystem_rc","--nohome_rc","--host_jvm_args=-Xmx4g","test","--config=x86_64",
             "--jobs=3","--strategy=TestRunner=local","--test_env=GO_TEST_WRAP_TESTV=1","--nocache_test_results",
             "--test_sharding_strategy=disabled","--runs_per_test=1","--flaky_test_attempts=1","--test_timeout=360",
             "--keep_going","--remote_executor=","--remote_cache=","--bes_backend=","--zip_undeclared_test_outputs",
             "--test_filter=^"+case+"$","--build_event_json_file="+str(bep),"--build_event_json_file_path_conversion=false",*TARGETS]
        scope=report["arms"][typ]={"test_results":{}}
        with (OUT/(typ+".log")).open("w") as log:
            scope["exit"]=subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT).returncode
        if scope["exit"]:
            scope["bazel_errors"]=[s.strip()[:420] for s in (OUT/(typ+".log")).read_text(errors="replace").splitlines()
                                    if s.startswith("ERROR:") or re.search(r"[.]go:\d+:\d+:",s)][:12]
        if bep.is_file():
            for line in bep.open():
                event=json.loads(line)
                if "started" in event: scope["invocation"]=event["started"]["uuid"]
                if "testResult" not in event: continue
                ident=event["id"]["testResult"]
                owner=ident["label"]
                assert owner in TARGETS and owner not in scope["test_results"]
                r=scope["test_results"][owner]={"ident":ident}
                try:
                    collect(r,event,typ)
                    r["verified"]=True
                except Exception as exc:
                    r["error"]=type(exc).__name__+" "+str(exc)[:900]
        save(report)
    report["verified"]=all(set(s["test_results"])==set(TARGETS) and all(r.get("verified") for r in s["test_results"].values()) for s in report["arms"].values())
    save(report)
    return not report["verified"]


if __name__=="__main__":
    raise SystemExit(main())
