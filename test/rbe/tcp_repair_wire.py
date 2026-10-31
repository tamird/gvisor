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
receipt = {"arm":arm, "source":(dest/"source").read_text().strip(), "exit":int((dest/"exit").read_text()),"cases":{}}
try:
    events = [json.loads(line) for line in (dest/"bep.jsonl").read_text().splitlines()]
    tests = [e["testResult"] for e in events if "testResult" in e]
    assert len(tests)==1, ("TestRunner count",len(tests))
    run = tests[0]
    assert not run.get("cachedLocally") and not run["executionInfo"].get("cachedRemotely") and run["executionInfo"]["strategy"] in ("local","standalone"),run
    receipt["invocation"] = next(e["started"]["uuid"] for e in events if "started" in e)
    log = (root/"test.log").read_text()
    (dest/"test.log").write_text(log)
    caps=[(p.name,p.read_bytes()) for p in root.rglob("*.pcap")]
    for p in root.rglob("outputs.zip"):
        with ZipFile(p) as z:caps.extend((n,z.read(n)) for n in z.namelist() if n.endswith(".pcap"))
    assert caps,"no capture"
    unique={hashlib.sha256(raw).hexdigest():raw for _,raw in caps}
    for sha,raw in unique.items():(dest/("trace-"+sha[:9]+".pcap")).write_bytes(raw)
    for case,base,k in (("repeat_high",1,4),("repeat_low",4,2)):
        records={int(tag):dict(cc=cc,ca=int(ca),reord=int(flag),cwnd=int(cwnd),total_retrans=int(n))
                 for tag,cc,ca,flag,cwnd,n in re.findall(r"tag=(\d+) cc=(\w+) ca=(\d+) reord=(\d+) cwnd=(\d+) total_retrans=(\d+)",log) if base<=int(tag)<=base+2}
        oldest=[(int(ca),int(fl),int(cw),int(n)) for ci,ca,fl,cw,n in re.findall(r"oldest case=(\w+) ca=(\d+) reord=(\d+) cwnd=(\d+) total_retrans=(\d+)",log) if ci==case]
        row={"tcp_info":records,"oldest_retransmit_states":oldest}
        receipt["cases"][case]=row
        assert list(records)==list(range(base,base+3)) and all(x["cc"]=="cubic" for x in records.values()),(case,records)
        assert records[base]["reord"]==records[base+1]["reord"]==0 and oldest and oldest[-1][0]==4 and oldest[-1][1]==0,(case,records,oldest)
        choices=[]
        for sha,raw in unique.items():
            frames=decode(raw)
            marks=[(j,r) for j,r in enumerate(frames) if r["sack"] and r["tag"] in range(base,base+3)]
            if len(marks)==3:choices.append((sha,frames,marks))
        assert len(choices)==1,(case,len(choices))
        sha,frames,marks=choices[0]
        first=marks[0][1]
        rel=lambda n:(n-first["ack"])%2**32
        mss=(first["sack"][0][1]-first["sack"][0][0])%2**32
        assert mss>0
        assert [r["tag"] for _,r in marks]==list(range(base,base+3))
        assert [rel(r["ack"]) for _,r in marks]==[0,0,mss]
        assert [[(rel(a),rel(b)) for a,b in r["sack"]] for _,r in marks]==[[(4*mss,5*mss),(2*mss,3*mss)],[(2*mss,3*mss)],[(k*mss,(k+1)*mss)]]
        def sends(until):
            return [(rel(r["seq"]),r["length"],round(1000*(r["ts"]-first["ts"]),3)) for r in frames[:until]
                    if (r["sport"],r["dport"])==(first["dport"],first["sport"]) and r["length"]>0 and rel(r["seq"])<5*mss]
        originals=sends(marks[0][0]); wire=sends(marks[-1][0]);before_repeat=sends(marks[1][0])
        assert [(start,size) for start,size,_ in originals]==[(i*mss,mss) for i in range(5)],(case,originals)
        assert [(a,b) for a,b,_ in before_repeat]==[(a,b) for a,b,_ in originals],(case,before_repeat)
        assert len([t for start,size,t in wire if start==0 and size==mss and t>0])>0,(case,wire)
        covered=[(s,n,t) for s,n,t in wire if s<(k+1)*mss and s+n>k*mss]
        assert len(covered)==1,(case,"repeated range was retransmitted before the ACK",covered)
        row.update(observed=True,pcap_sha256=sha,mss=mss,pre_ack_sends=wire,relative_ack=[rel(r["ack"]) for _,r in marks],relative_sacks=[[(rel(a),rel(b)) for a,b in r["sack"]] for _,r in marks])
    flags=[r["reord"] for case,row in receipt["cases"].items() for r in row["tcp_info"].values()]
    assert receipt["exit"]==(3 if any(flags) else 0),(receipt["exit"],flags)
    receipt["observed"]=True
except (AssertionError,IndexError,KeyError,OSError,ValueError,StopIteration) as e:
    receipt["observed"]=False; receipt["reason"]=repr(e)[:1000]
(dest/"result.json").write_text(json.dumps(receipt,indent=2)+"\n"); print(json.dumps(receipt))
