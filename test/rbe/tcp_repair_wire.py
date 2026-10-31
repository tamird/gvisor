from pathlib import Path
from zipfile import ZipFile
import hashlib,json,re,sys
from wire_decode import decode
name,root,dest=sys.argv[1:];root,dest=Path(root),Path(dest)
caps=[p.read_bytes() for p in root.rglob('*.pcap')]
for p in root.rglob('outputs.zip'):
    with ZipFile(p) as z:caps.extend(z.read(k) for k in z.namelist() if k.endswith('.pcap'))
captures={}
for raw in caps:
    sha=hashlib.sha256(raw).hexdigest()
    if sha not in captures:
        (dest/('trace-'+sha[:9]+'.pcap')).write_bytes(raw);captures[sha]=decode(raw)
log=(dest/'test.log').read_text(errors='replace')
rows={}
for variant,start,cap,sacked,replays,nbytes in re.findall(r'RECEIVER variant=(\w+) first=(\d+) rcvbuf=(\d+) sacked=(\d+) replayed=\[([0-9 ]*)\] bytes=(\d+)',log):
    assert variant not in rows and nbytes=='15360' and sacked=='16'
    start=int(start);off=lambda x:(x-start)%2**32
    replay=[int(x) for x in replays.split()]
    assert bool(replay)==(variant=='shrink')
    hits=[]
    for sha,pkts in captures.items():
        initial=[(p['sport'],p['dport']) for p in pkts if off(p['seq'])==480 and p['length']==480]
        if not initial:continue
        assert len(set(initial))==1
        pair=initial[0]
        tx=[(i,off(p['seq'])//480) for i,p in enumerate(pkts) if (p['sport'],p['dport'])==pair and p['length']==480 and off(p['seq'])<15360]
        assert [n for _,n in tx[:16]]==list(range(1,32,2))
        assert [n for _,n in tx[16:] if n%2]==replay
        for ix,(pi,seg) in enumerate(tx[:16]):
            assert any((p['dport'],p['sport'])==pair and off(p['ack'])==0 and (seg*480,(seg+1)*480) in [(off(a),off(b)) for a,b in p['sack']] for p in pkts[pi+1:tx[ix+1][0]]),(variant,seg)
        for tix,(pi,seg) in enumerate(tx[16:],16):
            if seg%2:assert any((p['dport'],p['sport'])==pair and off(p['ack'])==seg*480 for p in pkts[tx[tix-1][0]+1:pi]),(variant,seg,'peer did not stop at reported data')
        assert any((p['dport'],p['sport'])==pair and off(p['ack'])==15360 for p in pkts[tx[-1][0]+1:])
        hits.append(sha)
    assert len(hits)==1,(variant,hits)
    rows[variant]={'pcap':hits[0],'reported':16,'replayed':replay,'bytes_delivered':15360,'rcvbuf':int(cap)}
assert list(rows)==['control','shrink'] and rows['control']['rcvbuf']>rows['shrink']['rcvbuf'],rows
(dest/'result.json').write_text(json.dumps(rows,indent=2)+'\n')
