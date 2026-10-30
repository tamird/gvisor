"""Report each source's repair timing separately from its receiver-goodput share."""
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
from statistics import median

out=Path(os.environ['RUNNER_TEMP'])/'tcp-public'
original=Path(os.environ['RUNNER_TEMP'])/'qualification/current-shared'
v=json.loads((out/'verdict.json').read_text())
episodes=json.loads((out/'episodes.json').read_text())
summary={'source':v['heads'],'harness':v['harness'],'completed':datetime.now(timezone.utc).isoformat(),'rows':{},'paired':{},'missing':v['missing'],'failures':v['failures'],'driver':v['driver']}
for key,entry in v['passed'].items():
    arm,mode,name=key.split('/')
    row={'source':v['heads'][0 if arm.endswith('-before') else 1], 'exit':entry['exit'],'start':entry['start'],'end':entry['end'],'share':entry['netstack_share_bounds'],'mbps':entry['mbps_bounds'],'overlap':entry['overlap_seconds'],'flows':{}}
    raw=original/key/'results'
    dst=out/arm/(name+'-sources');dst.mkdir()
    row['raw_inputs']={}
    for path in ['client.1.pcap','client2.1.pcap','server.1.pcap','client.1-capture.txt','client2.1-capture.txt','server.1-capture.txt','primary/receiver.json','secondary/receiver.json','primary/flow-phases.tsv','secondary/flow-phases.tsv','primary/flow-settings.txt','secondary/flow-settings.txt']:
        buf=(raw/path).read_bytes()
        row['raw_inputs'][path]=dict(sha256=sha256(buf).hexdigest(),size=len(buf))
        dest=dst/path.replace('/','-')
        if path.endswith('.pcap'):
            dest.with_suffix('.pcap.gz').write_bytes(gzip.compress(buf,compresslevel=3,mtime=0))
        else:dest.write_bytes(buf)
    for side,details in episodes['cases'][key].items():
        epochs=details.get('first_drops',[])
        deltas=[e['third_to_retransmit_us'] for e in epochs if e.get('third_to_retransmit_us') is not None]
        measurable=[d for d in deltas if d>=0]
        received=[json.loads(l) for l in (out/arm/(mode+'-'+name+'-'+side+'-samples.jsonl')).read_text().splitlines()]
        fields=('ns','ca','reord','reownd_us','rack_rtt_us','recovery','out','cwnd','sacked','fack','una','seq_flight','wnd')
        transition=[{'before':{k:prior[k] for k in fields if k in prior},'after':{k:sample[k] for k in fields if k in sample}} for prior,sample in zip(received,received[1:]) if 'reord' in sample and bool(sample['reord'])!=bool(prior['reord'])]
        row['flows'][side]={'order':details['in_order'],'losses':{d:details.get(d) for d in ['forward','reverse']},'first_loss_epochs':len(epochs),'pre_threshold':sum(d<0 for d in deltas),'no_third_sack_while_hole_open':sum(e.get('retransmit_us') is not None and e.get('third_to_retransmit_us') is None for e in epochs),'no_observed_repair':sum(e.get('retransmit_us') is None for e in epochs),'eligible':len(measurable),'over_10ms':sum(d>10000 for d in measurable),'median_ms':median(measurable)/1000 if measurable else None,'reord_samples':sum(bool(s.get('reord')) for s in received) if side=='netstack' else None,'samples':len(received),'first_sample_reordered':bool(received[0].get('reord')) if side=='netstack' else None,'transitions':transition}
    summary['rows'][key]=row
for cc in ['reno','cubic']:
    data=[]
    for p in ['a','b']:
        names=[p+'-'+v+'/measured/'+cc+'-netstack' for v in ['before','amended']]
        if any(n not in summary['rows'] for n in names):continue
        before,after=(summary['rows'][n] for n in names)
        delays=[r['flows']['netstack']['median_ms'] for r in (before,after)]
        data.append({'pair':p,'repair_shorter':None if None in delays else delays[1]<delays[0],'share_higher':after['share'][0]>before['share'][1],'amended_at_least_half':after['share'][0]>=0.5})
    summary['paired'][cc]=data
(out/'paired-comparison.json').write_text(json.dumps(summary,indent=2)+'\n')
assert len(summary['rows'])==8 and all(r['exit']==0 for r in summary['rows'].values()),summary['missing']
