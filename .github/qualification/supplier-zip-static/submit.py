"""Submit or reattach to this one source-bound supplier run and return its verdict."""
import datetime
import getpass
import hashlib
import http.client
import json
from pathlib import Path
import sys
import uuid
sys.dont_write_bytecode=True
here=Path(__file__).resolve().parent
sys.path.insert(0,str(here.parent.parent))
import qualification_report as report

def main(read_key=getpass.getpass):
    digest='15a52682dbd12af6ff738472ac096f6434b02bf851b6cb072dbe0f98def2df99'
    source='9107384786056cdc05c9ad869bc07a577c66e746'
    host='cb548161f537465fda2576793e6d6cea59a32260'
    raw=(here/'request.json').read_bytes();req=json.loads(raw);plan=json.loads((here/'plan.json').read_text())
    assert len(raw)==43900 and hashlib.sha256(raw).hexdigest()==digest
    assert req['steps']==[{'run':(here/'request.sh').read_text()}] and req['repo']=='https://github.com/tamird/gvisor' and req['commit_sha']==host and req['branch']=='qualification/final-public-ci'
    assert report.hosted_run_preflight(req)==plan['preflight'] and plan['config']['source']==source
    assert json.loads((here/'entry.json').read_bytes())['request']==plan['request']
    marker=here/'attempt.json';submitted=here/'submitted.json'
    if marker.exists() and not submitted.exists():raise SystemExit('Submission already attempted: reconcile the original result.')
    class Key:
        def __init__(self):self.value=read_key('One rules_pkg ZIP writer comparison (hidden): ').strip()
        def read_text(self):return self.value
    key=Key();assert key.value
    def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
    if not submitted.exists():
        with marker.open('x') as f:json.dump({'request':digest,'attempt':now()},f)
        c=http.client.HTTPSConnection('app.buildbuddy.io',timeout=180)
        try:
            c.request('POST','/api/v1/Run',raw,{'Content-Type':'application/json','x-buildbuddy-api-key':key.value})
            resp=c.getresponse();data=resp.read(65537);assert len(data)<=65536
            if resp.status!=200:raise SystemExit('Run rejected (HTTP '+str(resp.status)+'): '+data.decode(errors='replace').replace(key.value,'[REDACTED]')[:600])
            val=json.loads(data);inv=val.get('invocationId',val.get('invocation_id'));assert str(uuid.UUID(inv))==inv
            with submitted.open('x') as f:json.dump({'invocation_id':inv,'source':source,'request':digest,'accepted':now()},f)
        finally:c.close()
    s=json.loads(submitted.read_bytes());assert s['source']==source and s['request']==digest
    print('https://app.buildbuddy.io/invocation/'+s['invocation_id'],flush=True)
    # native-receipt's callable supports distinct initial-host and executed-source
    # identities. Preserve its complete receipt and its CLI's non-PASS exit.
    result=report.native_receipt(s['invocation_id'],key,here/'qualification.tar.gz',here/'receipt.json',source,None,3700,expected_sources=[source],hosted_source=host)
    print(result['status']+': '+str(here/'receipt.json'),flush=True)
    return int(result['status']!='PASS')

if __name__ == '__main__':
    raise SystemExit(main())
