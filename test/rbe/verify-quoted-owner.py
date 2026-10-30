from datetime import datetime,timezone
import json,re,sys
from pathlib import Path
d=Path(sys.argv[1])
def read(n):
    return (d/n).read_text().strip() if (d/n).is_file() else ''
records=re.findall(r'(?m)^\s*--- (PASS|FAIL|SKIP): (\S+)',read('test.log'))
source=read('source');target=read('target')
errors=[]
if source!='9bc5517f1f05b62025af3ae4846eaae8431bcc79' or target!='//pkg/tcpip/network/ipv6:ipv6_test':
    errors.append('missing or unexpected source or target')
if read('format.exit')!='0' or 'clang-format' not in read('format.log'):
    errors.append('repository formatter did not pass')
if read('exit')!='0' or not (d/'test.xml').is_file() or not records or any(x[0]=='FAIL' for x in records):
    errors.append('IPv6 test missing or failing')
out={'verified':not errors,'errors':errors,'source':source,'target':target,'records':records,'started':read('start'),'finished':read('end'),'verified_at':datetime.now(timezone.utc).isoformat()}
(d/'verdict.json').write_text(json.dumps(out,indent=2)+'\n')
print(len(records),errors)
sys.exit(bool(errors))
