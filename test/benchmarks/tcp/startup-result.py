"""Keep original evidence and classify each synchronized launch using the two taps."""
from datetime import datetime, timezone
import csv
import gzip
import hashlib
import io
import json
import os
from pathlib import Path

root = Path(os.environ['RUNNER_TEMP'])/'tcp-public'
original = Path(os.environ['RUNNER_TEMP'])/'qualification/current-shared'
verdict = json.loads((root/'verdict.json').read_text())
episodes = json.loads((root/'episodes.json').read_text())
old = {
  'a-amended/measured/reno-netstack': [30.34,33.28],
  'a-amended/measured/cubic-netstack': [28.86,31.99],
  'b-amended/measured/reno-netstack': [24.92,28.95],
  'b-amended/measured/cubic-netstack': [31.15,34.03],
}
result = dict(heads=verdict['heads'], driver=verdict['driver'], missing=verdict['missing'],
              failures=verdict['failures'], rows={}, finished_at=datetime.now(timezone.utc).isoformat())

def packet_connections(path, peer):
    ports = {}
    with gzip.open(path,'rt') as f:
        for p in csv.DictReader(f,delimiter='\t'):
            sport=int(p['tcp.srcport']);dport=int(p['tcp.dstport'])
            if peer not in (sport,dport):
                continue
            forward=(peer==dport)
            target=ports.setdefault(sport if forward else dport,{})
            t=round(float(p['frame.time_epoch'])*1e6)
            flags=int(p['tcp.flags'],0);size=int(p['tcp.len'])
            if flags&2:
                target.setdefault('syn' if forward else 'synack',t)
            if forward and flags==16:
                target.setdefault('handshake_ack',t)
            if size:
                target.setdefault('first_data' if forward else 'first_response',t)
                if forward and size>=1000:
                    target.setdefault('first_bulk',t)
    return ports

for key,v in verdict['passed'].items():
    arm,mode,case=key.split('/')
    public=root/arm
    raw=original/key/'results'; dst=public/(case+'-sources')
    dst.mkdir(exist_ok=True)
    files=['client.1.pcap','client2.1.pcap','server.1.pcap','client.1-capture.txt','client2.1-capture.txt','server.1-capture.txt']
    for role in ['primary','secondary']:
        files.extend(role+'/'+f for f in ['receiver.json','flow-phases.tsv','flow-settings.txt','iperf-attempts.tsv'])
    row=dict(source=verdict['heads'][1], exit=v['exit'], share=v['netstack_share_bounds'],
             old_share_pct=old[key], mbps=v['mbps_bounds'], overlap=v['overlap_seconds'],
             starts={},raw_inputs={}, clocks={}, attempts={},diagnostics=[], flows={})
    result['rows'][key]=row
    for name in files:
        data=(raw/name).read_bytes()
        row['raw_inputs'][name]=dict(sha256=hashlib.sha256(data).hexdigest(),size=len(data))
        out=dst/name.replace('/','-')
        if name.endswith('.pcap'):
            out=out.with_suffix('.pcap.gz'); data=gzip.compress(data,compresslevel=3,mtime=0)
        out.write_bytes(data)
    starts={}
    for side,client,role in [('netstack','client.1','primary'),('linux','client2.1','secondary')]:
        wire=v['wire'][side];port=wire['client_port'];peer=wire['wan_port']
        clocks={a['phase']:a['unix_seconds'] for a in csv.DictReader((raw/role/'flow-phases.tsv').open(),delimiter='\t')}
        row['clocks'][side]=clocks
        attempt_rows=list(csv.DictReader((raw/role/'iperf-attempts.tsv').open(),delimiter='\t'))
        row['attempts'][side]=attempt_rows
        if [a['result'] for a in attempt_rows]!=['0']:
            row['diagnostics'].append(side+' did not succeed on exactly one connection attempt')
        if not all(s in clocks for s in ['proxy-listener-ready','both-proxies-ready','client-operation-begin','client-operation-end']):
            row['diagnostics'].append('missing launch phase for '+side)
        for tap,device in [('sender',client),('receiver','server.1')]:
            parts=packet_connections(public/f'{mode}-{case}-{device}-packets.tsv.gz',peer)
            if port not in parts or 'first_bulk' not in parts[port]:
                row['diagnostics'].append('no dominant bulk at '+tap+' for '+side)
                continue
            row['starts'][side+'/'+tap]=dict(data_port=port,connections=parts)
            starts[side,tap]=parts[port]['first_bulk']
        row['flows'][side]={k:episodes['cases'][key][side].get(k) for k in ['in_order','forward','reverse','first_drops','epoch_sack_to_retransmit_us']}
    deltas={}
    for tap in ['sender','receiver']:
        if ('netstack',tap) not in starts or ('linux',tap) not in starts:continue
        deltas[tap]=starts['netstack',tap]-starts['linux',tap]
        if abs(deltas[tap])>15000:
            row['diagnostics'].append(tap+' first-bulk skew exceeds 15 ms')
    row['first_bulk_gvisor_minus_linux_us']=deltas
    row['matched']=not row['diagnostics'] and v['exit']==0 and len(deltas)==2
    print(key,row['first_bulk_gvisor_minus_linux_us'],row['matched'],row['share'],row['diagnostics'])
(root/'startup-verdict.json').write_text(json.dumps(result,indent=2)+'\n')
assert set(result['rows'])==set(old) and not result['missing'] and not result['failures'] and result['driver']['status']==0
