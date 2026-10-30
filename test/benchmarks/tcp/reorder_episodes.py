"""Match wire observations of the same connection before and after its WAN."""
import bisect
from collections import Counter, defaultdict
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

source = Path(sys.argv[1])
output = Path(sys.argv[2])
archive = zipfile.ZipFile(source) if source.is_file() else None
def contents(name):
    return archive.read(name) if archive else (source / name).read_bytes()
v = json.loads(contents("verdict.json"))
def prefix(name):
    parts=name.split("/")
    return "/".join([*parts[:-2],"-".join(parts[-2:])])

def read(name):
    rows=csv.DictReader(io.StringIO(gzip.decompress(contents(name)).decode()),delimiter="\t")
    return [(round(float(r["frame.time_epoch"])*1e6),int(r["tcp.srcport"]),int(r["tcp.dstport"]),int(r["tcp.seq_raw"]),int(r["tcp.ack_raw"]),int(r["tcp.len"]),int(r["tcp.flags"],0),r["tcp.options.sack_le"],r["tcp.options.sack_re"]) for r in rows]

def pct(rows):
    vals=sorted(rows)
    return [vals[round((len(vals)-1)*q)] if vals else None for q in (0,0.25,0.5,0.75,0.9,1)]

def matching(source,sink):
    loc=defaultdict(list)
    for i,p in enumerate(source):loc[p[1:]].append(i)
    earliest=[]
    left=-1
    for p in sink:
        candidates=loc[p[1:]]
        j=bisect.bisect_right(candidates,left)
        if j==len(candidates):return None
        left=candidates[j]; earliest.append(left)
    latest=[]
    right=len(source)
    for p in reversed(sink):
        candidates=loc[p[1:]]
        j=bisect.bisect_left(candidates,right)-1
        if j<0:return None
        right=candidates[j];latest.append(right)
    latest.reverse()
    possible=set()
    exact=[]
    for p,a,b in zip(sink,earliest,latest):
        if a==b:exact.append((a,p[0]-source[a][0]));possible.add(a)
        else:
            candidates=loc[p[1:]]
            possible.update(candidates[bisect.bisect_left(candidates,a):bisect.bisect_right(candidates,b)])
    return (set(range(len(source)))-possible,exact)

def summary(case,wire,client,server,lane):
    port=wire["client_port"];peer=wire["wan_port"]
    data=lambda rows:[p for p in rows if p[1:3]==(port,peer) and p[5]>0]
    acks=lambda rows:[p for p in rows if p[1:3]==(peer,port) and p[6]&16]
    src=data(client); dst=data(server); txack=acks(server); rxack=acks(client)
    forward=matching(src,dst);reverse=matching(txack,rxack)
    if forward is None or reverse is None:
        return {"in_order": [forward is not None,reverse is not None]}
    loss,delivered=forward
    rloss,rdelivered=reverse
    syn=next(p for p in client if p[1:3]==(port,peer) and p[6]&2)
    isn=(syn[3]+1)&0xffffffff
    rel=lambda n: (n-isn)&0xffffffff
    times=[p[0] for p in src]
    akts=[p[0] for p in rxack]
    seqs=[rel(p[3]) for p in src]
    highs=[]
    high=0
    originals=[]
    for p,n in zip(src,seqs):
        originals.append(n>=high)
        high=max(high,n+p[5]);highs.append(high)
    extent=max(highs)
    edges=[int(x) for p in rxack for fld in (p[7],p[8]) for x in fld.split(',') if x]
    if edges:
        relative=all(1<=x<=extent+1 for x in edges);raw=all(rel(x)<=extent for x in edges)
        assert raw!=relative
    def sacks(p):
        cv=(lambda n:n-1) if relative else rel
        return [(cv(int(a)),cv(int(b))) for a,b in zip(p[7].split(','),p[8].split(',')) if a and b]
    d={"in_order":[True,True],"forward":{"sent":len(src),"received":len(dst),"definite_loss":len(loss),"unattributed_missing":len(src)-len(dst)-len(loss),"transit_us":pct([t for _,t in delivered])},"reverse":{"sent":len(txack),"received":len(rxack),"definite_loss":len(rloss),"unattributed_missing":len(txack)-len(rxack)-len(rloss),"transit_us":pct([t for _,t in rdelivered])}}
    d["burst"]={}
    for span in [150,300,600,1200]:
        counts=[i-bisect.bisect_left(times,t-span) for i,t in enumerate(times)]
        trials=Counter((n>=3, i in loss) for i,n in enumerate(counts))
        d["burst"][span]={"sent_with_prior3":sum(1 for x in counts if x>=3),"lost_with_prior3":sum(1 for i in loss if counts[i]>=3),"prior_count":pct(counts),"lost_prior_count":pct([counts[i] for i in loss])}
    original_ranges=[(x,x+p[5]) for x,p,ok in zip(seqs,src,originals) if ok]
    original_starts=[x for x,end in original_ranges]
    samples=[]
    if case.split("/")[-2] == "measured":
        samples=[json.loads(l) for l in contents(prefix(case)+"-"+lane+"-samples.jsonl").splitlines()]
        samples=[r for r in samples if r["port"]==port]
    if samples and "reownd_us" in samples[0]:
        d["window_us"]=pct([r["reownd_us"] for r in samples])
        d["reordered_samples"]=sum(r["reord"] for r in samples)
    drops=sorted(loss);epochs=[]
    for pos,i in enumerate(drops):
        if pos and times[i]-times[drops[pos-1]]<=100_000:continue
        if not originals[i]:continue
        p=src[i];x=seqs[i];end=x+p[5]
        ack_idx=bisect.bisect_right(akts,p[0])-1
        preack=rel(rxack[ack_idx][4]) if ack_idx>=0 else 0
        saw=None;succ=None
        for a in rxack[ack_idx+1:]:
            if rel(a[4])>=end:succ=a;break
            if saw is None and any(lo>=end for lo,hi in sacks(a)):saw=a
        repeat=next((src[j] for j in range(i+1,len(src)) if seqs[j]<=x<seqs[j]+src[j][5]),None)
        row={"time":p[0],"seq":x,"len":p[5],"flight":highs[i]-preack,"in_600us":i+1-bisect.bisect_left(times,p[0]-600),"sack_us":None if saw is None else saw[0]-p[0],"retransmit_us":None if repeat is None else repeat[0]-p[0],"acked_us":None if succ is None else succ[0]-p[0],"sack_to_retransmit_us":None if saw is None or repeat is None else repeat[0]-saw[0]}
        if repeat:
            seen=set()
            third=None
            for a in rxack[ack_idx+1:]:
                if a[0]>repeat[0] or rel(a[4])>=end:break
                for lo,hi in sacks(a):
                    j=bisect.bisect_left(original_starts,max(lo,end))
                    while j<len(original_ranges) and original_ranges[j][1]<=hi:
                        seen.add(j);j+=1
                if len(seen)>=3:
                    third=a;break
            row["third_to_retransmit_us"]=None if third is None else repeat[0]-third[0]
            if third:
                inner=[r for r in samples if third[0]*1000<=r["ns"]<=repeat[0]*1000]
                row["samples_in_wait"]=len(inner)
                if lane=="netstack" and samples and "reownd_us" in samples[0]:
                    nearby=[r for r in samples if abs(r["ns"]-third[0]*1000)<=2_000_000]
                    if nearby:
                        close=min(nearby,key=lambda r:abs(r["ns"]-third[0]*1000))
                        row["threshold_sample"]={k:close[k] for k in ("ns","reord","reownd_us","rack_rtt_us","sacked","fack","recovery","out","cwnd","una","seq_flight","wnd")}
                        row["sample_minus_third_us"]=round(close["ns"]/1000-third[0])

                if inner:
                    row["wait_sample"]= {k:inner[-1][k] for k in ("ns","cwnd","out","sacked","wnd","queued","seq_flight","recovery","rtt_us","ca","reord","reownd_us","rack_rtt_us","fack") if k in inner[-1]}
        epochs.append(row)
    d["first_drops"]=epochs
    d["epoch_sack_to_retransmit_us"]=pct([e["sack_to_retransmit_us"] for e in epochs if e["sack_to_retransmit_us"] is not None])
    d["epoch_ack_us"]=pct([e["acked_us"] for e in epochs if e["acked_us"] is not None])
    return d

out={"source":v["upstream"],"harness":v["harness"],"cases":{}}
if archive:out["zip_sha256"]=hashlib.sha256(source.read_bytes()).hexdigest()
for name,case in v["passed"].items():
    stem=prefix(name)
    server=read(stem+"-server.1-packets.tsv.gz")
    out["cases"][name]={}
    for lane,w in case["wire"].items():
        client=read(stem+("-client.1-" if lane=="netstack" else "-client2.1-")+"packets.tsv.gz")
        d=summary(name,w,client,server,lane)
        out["cases"][name][lane]=d
        print(name,lane,d["in_order"],d.get("forward"),d.get("reverse"),str(d.get("burst",{}).get(600,""))[:145], d.get("epoch_sack_to_retransmit_us"))
output.write_text(json.dumps(out,indent=2)+"\n")
if v.get("heads"):
    assert len(out["cases"])==4, list(out["cases"])
    # Preserve per-direction ordering diagnostics. The analyzer omits
    # loss timing for traces whose observed packet order cannot be reconciled.
