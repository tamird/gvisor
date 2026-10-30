from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
from zipfile import ZipFile

def decode(raw):
    magic = raw[:4]
    endian = {b"\xd4\xc3\xb2\xa1":"<",b"\xa1\xb2\xc3\xd4":">"}[magic]
    assert struct.unpack_from(endian+"I",raw,20)[0]==1
    pos=24
    packets=[]
    while pos+16<=len(raw):
        sec,usec,cap,size=struct.unpack_from(endian+"IIII",raw,pos)
        frame=raw[pos+16:pos+16+cap];pos+=16+cap
        if frame[12:14]!=b"\x08\x00":continue
        ip=frame[14:]
        if len(ip)<20 or ip[9]!=6:continue
        iph=(ip[0]&15)*4
        tcp=ip[iph:]
        if len(tcp)<20:continue
        sport,dport,sequence,ack=struct.unpack_from("!HHII",tcp)
        th=(tcp[12]>>4)*4
        length=int.from_bytes(ip[2:4],"big")-iph-th
        sack=[]
        opt=20
        while opt<th:
            kind=tcp[opt]
            if kind==0:break
            if kind==1:opt+=1;continue
            n=tcp[opt+1]
            if kind==5:sack=[struct.unpack_from("!II",tcp,i) for i in range(opt+2,opt+n,8)]
            assert n>=2;opt+=n
        packets.append(dict(ts=sec+usec/1e6,sport=sport,dport=dport,seq=sequence,ack=ack,length=length,sack=sack,tag=tcp[th] if length==1 else None))
    return packets

def wire(root, dest):
    caps=[(p.name,p.read_bytes()) for p in root.rglob("*.pcap")]
    for p in root.rglob("outputs.zip"):
        with ZipFile(p) as z:caps.extend((n,z.read(n)) for n in z.namelist() if n.endswith(".pcap"))
    assert len(caps)==1, [n for n,_ in caps]
    name,raw=caps[0];(dest/"trace.pcap").write_bytes(raw);packets=decode(raw)
    tagged=[(i,p) for i,p in enumerate(packets) if p["tag"] in (1,2) and p["sack"]]
    groups={}
    for i,p in tagged:groups.setdefault((p["sport"],p["dport"]),[]).append((i,p))
    assert sorted([p["tag"] for _,p in v] for v in groups.values())==[[1,2],[2]]
    result=[]
    for (sport,dport),samples in groups.items():
        i,last=samples[-1];lo,hi=last["sack"][0];mss=(hi-lo)&0xffffffff
        two=len(samples)==2;n=6 if two else 3;ack_segments=4 if two else 1
        first=(hi-n*mss)&0xffffffff
        assert mss>0 and all(len(p["sack"])==1 and p["sack"][0]==(lo,hi) and (p["ack"]-first)&0xffffffff==(0 if p["tag"]==1 else ack_segments*mss) for _,p in samples)
        seqs=[(idx,(p["seq"]-first)&0xffffffff) for idx,p in enumerate(packets[:i]) if p["sport"]==dport and p["dport"]==sport and p["length"]==mss and (p["seq"]-first)&0xffffffff<n*mss]
        target=(ack_segments-1)*mss
        assert [off for idx,off in seqs if idx<samples[0][0]][:n]==list(range(0,n*mss,mss)),seqs
        assert [off for _,off in seqs].count(target)==1,seqs
        if not two:assert [off for _,off in seqs]==list(range(0,n*mss,mss)),seqs
        result.append({"tags":[p["tag"] for _,p in samples],"cumulative_offsets":[(p["ack"]-first)&0xffffffff for _,p in samples],"sack":[(n-1)*mss,n*mss],"transmissions":[off for _,off in seqs],"original_acked_below_prior_fack":target if two else None})
    return {"sha256":hashlib.sha256(raw).hexdigest(),"member":name,"flights":result}

arm,source,root,out=sys.argv[1:];root=Path(root);out=Path(out);dest=out/arm
r={"variant":arm,"source":source,"observed":False}
try:
    r["exit"]=int((dest/"exit").read_text())
    log=(root/"test.log").read_text(errors="replace")
    records=re.findall(r"ack=(\d+) sack=\[(\d+),(\d+)\) tcpi_reord_seen=(\d+) tcpi_total_retrans=(\d+)",log)
    r["observations"]=[dict(zip(("ack","sack_start","sack_end","reord_seen","total_retrans"),map(int,v))) for v in records]
    assert len(records)==3,r["observations"]
    r["subtests"]={name:status for status,name in re.findall(r"--- (PASS|FAIL): TestRACKReorderACKBoundary/(one_ack|two_acks)",log)}
    r["capture"]=wire(root,dest)
    events=[json.loads(l) for l in (dest/"bep.jsonl").read_text().splitlines()]
    results=[e["testResult"] for e in events if "testResult" in e]
    assert len(results)==1,results
    test=results[0];r["test_status"]=test["status"];r["execution"]=test["executionInfo"]
    assert not test.get("cachedLocally") and not test["executionInfo"].get("cachedRemotely") and test["executionInfo"]["strategy"] in ("local","standalone")
    r["observed"]=True
except Exception as err:
    r["error"]=type(err).__name__+": "+str(err)[:600]
r["finished"]=datetime.now(timezone.utc).isoformat()
(dest/"result.json").write_text(json.dumps(r,indent=2)+"\n")
print(r)
