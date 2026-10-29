"""Reconcile only the already-executed Linux test's log and declared capture."""
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback
from first_loss_pcap import records, wire_result

ROOT = Path(os.environ["RUNNER_TEMP"])
SOURCE = "dcae752b2f6177e9aba1d8e6192d16117267882e"
CHILD = "b4ea3241-2fee-4cdf-93f9-a039f905ab38"
SOCKETS = "2a930f4148ea21d95f9cbb24325d2ef41bbd9c98f18b2f2cd73e3d5ecb8aff27"
INPUTS = {"capture": ("3deb86fa647ed840ae92e7ce279ad3da67a4194cf7fa16a705fd828f82e79ebd",115570),
          "log": ("8414b0ab95749bf7ebeef82df50d0953d796af0d0da9b9c2b79e2f305a7c0930",5516)}


def transfer():
    from original_reader import request
    key = os.environ.pop("BUILDBUDDY_API_KEY")
    for name,(digest,size) in INPUTS.items():
        body = bytearray()
        count = request(key,"/api/v1/GetFile",{"uri":f"bytestream://remote.buildbuddy.io/blobs/{digest}/{size}"},size,body.extend)
        assert count == len(body) == size and hashlib.sha256(body).hexdigest() == digest
        (ROOT/name).write_bytes(body)


def verify(rows, sample):
    # Use the fixture's explicit start sequence and covered bytes at each
    # boundary. The current ACK, not the first flight's seq, delimits a window.
    head, mss = sample["RepairSeq"], sample["MSS"]
    def off(value):
        return ((value-head+(1<<31))%(1<<32))-(1<<31)
    [forward] = {r[0] for r in rows if r[1]==head and r[4]}
    reverse = (*forward[2:], *forward[:2])
    tx = [(i,off(seq),n,c,t) for i,(k,seq,_,_,n,c,_,t) in enumerate(rows) if k==forward and n]
    rx = [(i,off(ack),[(off(a),off(b)) for a,b in sacks],t) for i,(k,_,ack,flags,_,_,sacks,t) in enumerate(rows) if k==reverse and flags&16]
    try:
        original = wire_result(rows,sample,INPUTS["capture"][0])
        old = {"pass":original is not None}
    except (AssertionError, StopIteration, ValueError) as error:
        old = {"pass":False, "exception":type(error).__name__,"line":traceback.extract_tb(error.__traceback__)[-1].lineno}
    repairs = [i for i,s,n,_,_ in tx if s==0 and n==mss]
    sack = [next((i for i,ack,blocks,_ in rx if ack==0 and (mss,n*mss) in blocks),None) for n in (2,3,4)]
    recovery = len(repairs)==2 and all(i is not None for i in sack) and repairs[0]<sack[0]<sack[1]<sack[2] and sack[0]<repairs[1]
    i0, _, _, ts0 = next(r for r in rx if r[0]>repairs[-1] and r[1]==5*mss)
    flights=[]
    for slot,f in enumerate((sample["Prime"],*sample["Flights"])):
        start=off(f["Start"])
        end=start+f["Bytes"]
        ack = next((r for r in rx if r[0]>i0 and r[1]>=end),None)
        stop=ack[0] if ack else len(rows)
        segments=[(i,s,n,c,t) for i,s,n,c,t in tx if i0<i<stop and start<=s<end]
        gaps=[]
        cursor=start
        for i,s,n,c,t in segments:
            if s!=cursor: gaps.append([cursor-start,s-start])
            cursor=max(cursor,s+n)
        first=segments[0] if segments else None
        last=segments[-1] if segments else None
        advance=[a-start for i,a,_,_ in rx if last and i0<i<last[0] and a>start]
        extras=[[s-start,n] for i,s,n,_,_ in tx if last and i0<i<=last[0] and not start<=s<end]
        result={"offset":start,"bytes":f["Bytes"],"frames":len(segments),
                "declared_payload_bytes":sum(x[2] for x in segments),"captured_payload_bytes":sum(x[3] for x in segments),
                "wire_elapsed_ns":first[4]-ts0 if first else None,"gaps_or_overlaps":gaps,"early_acks":advance,"extras":extras,
                "next_ack":None if not ack else ack[1]-start,
                "covered":bool(segments and not gaps and not advance and not extras and cursor==end and (slot==5 or (ack and ack[1]==end)))}
        flights.append(result)
        if ack:
            i0,_,_,ts0=ack
    gap=flights[1]["wire_elapsed_ns"]
    actual_window=gap is not None and ((29_000_000_000<=gap<=32_000_000_000) if sample["IdleNS"] else 0<=gap<sample["PreRTO"]*1000)
    return {"qualified":bool(recovery and actual_window and all(f["covered"] for f in flights)),
            "original_predicate":old,"repair_frames":len(repairs),"sack_reports":sum(i is not None for i in sack),
            "wire_idle_ns":gap,"idle_in_window":actual_window,"prime":flights[0],"flights":flights[1:]}


def summarize():
    [f]=(ROOT/"socket").rglob("result.json")
    body=f.read_bytes();assert hashlib.sha256(body).hexdigest()==SOCKETS
    socket=json.loads(body)
    assert socket["source"]==SOURCE and socket["child"]==CHILD and socket["native_exit"]==0 and socket["remote"] and not socket["cached"] and socket["error"]["stage"]=="pcap"
    for name,(digest,size) in INPUTS.items():
        body=(ROOT/name).read_bytes();assert len(body)==size and hashlib.sha256(body).hexdigest()==digest
    rows=records((ROOT/"capture").read_bytes())
    result={"source":SOURCE,"child":CHILD,"native_exit":0,"original_status":socket["status"],
            "original_response":SOCKETS,"pcap":INPUTS["capture"],"test_log":INPUTS["log"],"modes":{}}
    for line in (ROOT/"log").read_text().splitlines():
        if "LINUX_IDLE_RESULT " not in line:
            continue
        sample=json.loads(line.split("LINUX_IDLE_RESULT ",1)[1]);mode=sample["Algorithm"]+"_"+sample["Profile"]
        public={k:v for k,v in sample.items() if k!="RepairSeq"}
        public["Prime"]={k:v for k,v in sample["Prime"].items() if k!="Start"}
        public["Flights"]=[{k:v for k,v in entry.items() if k!="Start"} for entry in sample["Flights"]]
        assert public=={k:v for k,v in socket["modes"][mode].items() if k!="wire"} and mode not in result["modes"]
        try:
            result["modes"][mode]=verify(rows,sample)
        except Exception as error:
            result["modes"][mode]={"qualified":False,"error":type(error).__name__,"line":traceback.extract_tb(error.__traceback__)[-1].lineno}
    result["status"]="PASS" if set(result["modes"])==set(socket["modes"]) and all(v["qualified"] for v in result["modes"].values()) else "INCOMPLETE"
    (ROOT/"packet-summary.json").write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
    return int(result["status"]!="PASS")


if __name__=="__main__":
    if sys.argv[1:]==["transfer"]:
        transfer()
    else:
        sys.exit(summarize())
