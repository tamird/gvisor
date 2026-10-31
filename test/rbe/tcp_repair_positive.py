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


arm, root_arg, dest_arg = sys.argv[1:]
root, dest = Path(root_arg), Path(dest_arg)
result = {"arm": arm, "exit": int((dest/"exit").read_text())}
try:
    events=[json.loads(line) for line in (dest/"bep.jsonl").read_text().splitlines()]
    tests=[v["testResult"] for v in events if "testResult" in v]
    assert len(tests)==1
    run=tests[0]
    assert not run.get("cachedLocally") and not run["executionInfo"].get("cachedRemotely") and run["executionInfo"]["strategy"] in ("local","standalone"),run
    log = (root/"test.log").read_text()
    result["observations"] = [(int(tag),int(value),int(retrans)) for tag,value,retrans in re.findall(r"tag=(\d+) tcpi_reord_seen=(\d+) tcpi_total_retrans=(\d+)",log)]
    assert len(result["observations"]) == 3, result
    caps=[(p.name,p.read_bytes()) for p in root.rglob("*.pcap")]
    for p in root.rglob("outputs.zip"):
        with ZipFile(p) as z:
            caps.extend((n,z.read(n)) for n in z.namelist() if n.endswith(".pcap"))
    assert len(caps)==1, [n for n,_ in caps]
    name,raw=caps[0]
    (dest/"trace.pcap").write_bytes(raw)
    frames=decode(raw)
    tagged=[(i,row) for i,row in enumerate(frames) if row["tag"] in (1,2,3) and row["sack"]]
    assert [row["tag"] for _,row in tagged]==[1,2,3], [(r["tag"],r["sack"]) for _,r in tagged]
    one=tagged[0][1]
    first=one["ack"]; mss=(one["sack"][0][1]-one["sack"][0][0])%2**32
    assert mss>0
    rel=lambda v: (v-first)%2**32
    ranges=[[(rel(a),rel(b)) for a,b in row["sack"]] for _,row in tagged]
    expect=[[(5,6),(2,3)],[(2,3)],[(2,4),(5,6)]]
    assert ranges==[[(a*mss,b*mss) for a,b in pairs] for pairs in expect],ranges
    assert all(row["ack"]==first and (row["sport"],row["dport"])==(one["sport"],one["dport"]) for _,row in tagged)
    def sends(until):
        return [rel(r["seq"]) for r in frames[:until] if r["length"]==mss and (r["sport"],r["dport"])==(one["dport"],one["sport"]) and rel(r["seq"])<6*mss]
    originals=sends(tagged[0][0])
    assert originals[:6]==[n*mss for n in range(6)], originals
    before_repeat=sends(tagged[1][0]); before_new=sends(tagged[2][0])
    assert before_repeat.count(2*mss)==1 and before_new.count(3*mss)==1,(before_repeat,before_new)
    result["wire"]={"first_transmissions":originals,"transmissions_before_repeat":before_repeat,"transmissions_before_genuine":before_new,"relative_sack_blocks":ranges,"mss":mss,"capture":name,"sha256":hashlib.sha256(raw).hexdigest()}
    bits=[bool(value) for _,value,_ in result["observations"]]
    result["expected_result"]=bits==([False,True,True] if arm=="rack" else [False,False,True]) and result["exit"]==(3 if arm=="rack" else 0)
    result["observed"]=True
except (AssertionError,IndexError,KeyError,OSError,ValueError) as err:
    result["observed"]=False
    result["reason"]=repr(err)[:950]
(dest/"result.json").write_text(json.dumps(result,indent=2)+"\n")
print(json.dumps(result))
