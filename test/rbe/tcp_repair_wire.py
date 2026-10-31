from pathlib import Path
from zipfile import ZipFile
import hashlib,json,sys
from wire_decode import decode
name,root,dest=sys.argv[1:];root=Path(root);dest=Path(dest)
caps=[p.read_bytes() for p in root.rglob('*.pcap')]
for p in root.rglob('outputs.zip'):
    with ZipFile(p) as z: caps.extend(z.read(k) for k in z.namelist() if k.endswith('.pcap'))
caps={hashlib.sha256(raw).hexdigest():raw for raw in caps}
rows={}
for sha,raw in caps.items():
    (dest/('trace-'+sha[:9]+'.pcap')).write_bytes(raw)
    packets=decode(raw)
    for ix,case in enumerate(('none','one_byte','whole_packet','sack_suffix','sack_whole_then_trim','suffix_trim_twice','whole_trim_twice')):
        tag=40+4*ix
        hit=[i for i,x in enumerate(packets) if x['tag']==tag]
        if not hit: continue
        assert len(hit)==1 and case not in rows,(case,hit)
        i,=hit; a=packets[i]; src=(a['sport'],a['dport'])
        markers=[j for j,p in enumerate(packets) if p['tag'] in (tag,tag+1,tag+2,tag+3) and (p['sport'],p['dport'])==src]
        extra=case in ('suffix_trim_twice','whole_trim_twice')
        assert [packets[x]['tag'] for x in markers]==([tag,tag+1,tag+3,tag+2] if extra else [tag,tag+1,tag+2])
        j=markers[1];k=markers[-1];b=packets[j];c=packets[k];last=packets[markers[-2]]
        if extra:
            assert last['ack']==(b['ack']+1)%2**32 and not last['sack']
        span=(c['ack']-a['ack'])%2**32;mss,odd=divmod(span,2)
        assert not odd and mss==480 and 0.14<=c['ts']-last['ts']<0.35,(mss,case,c['ts']-last['ts'])
        ack=(b['ack']-a['ack'])%2**32
        assert ack==(mss+mss//2 if case in ('sack_suffix','sack_whole_then_trim','suffix_trim_twice','whole_trim_twice') else mss)
        s=[((lo-a['ack'])%2**32,(hi-a['ack'])%2**32) for lo,hi in a['sack']]
        expected={'none':[],'one_byte':[(mss,mss+1)],'sack_suffix':[(ack,2*mss)],'suffix_trim_twice':[(ack,2*mss)]}.get(case,[(mss,2*mss)])
        assert s==expected and not (b['sack'] or c['sack']),(case,s,expected)
        sent=[(l,(p['seq']-a['ack'])%2**32,p['length']) for l,p in enumerate(packets) if (p['dport'],p['sport'])==src and p['length'] and (p['seq']-a['ack'])%2**32<2*mss]
        assert [(offset,size) for l,offset,size in sent if l<j]==[(0,mss),(mss,mss)],(case,sent,markers)
        retransmits=[{'start':offset,'size':size,'after_ms':round(1000*(packets[l]['ts']-b['ts']),3)} for l,offset,size in sent if j<l<k]
        rows[case]={'pcap':sha,'mss':mss,'ack':ack,'sack':s,'interval_ms':round(1000*(c['ts']-last['ts']),3),'retransmits':retransmits}
assert set(rows)=={'none','one_byte','whole_packet','sack_suffix','sack_whole_then_trim','suffix_trim_twice','whole_trim_twice'},rows
(dest/'result.json').write_text(json.dumps({'observed':True,'rows':rows},indent=2)+'\n')
