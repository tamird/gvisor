"""Load one exact Bazel-produced Docker archive retained by the original job."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

label, invocation, receipt, output = sys.argv[1:]
name = label.split(':')[-1] + '.tar'
headers = {'Content-Type': 'application/json', 'x-buildbuddy-api-key': os.environ['BUILDBUDDY_API_KEY']}

def get(method, payload, limit):
    request = Request('https://app.buildbuddy.io/api/v1/' + method, json.dumps(payload).encode(), headers)
    with urlopen(request, timeout=100) as response:
        data=response.read(limit+1)
    if len(data)>limit:
        raise ValueError('output exceeds limit')
    return data

data=json.loads(get('GetAction', {'selector': {'invocation_id':invocation, 'target_label':label}}, 2<<20))
refs={f['uri'] for a in data.get('action', []) if a.get('targetLabel',a.get('target_label'))==label for f in a.get('file',[]) if Path(f['name']).name==name}
if len(refs)!=1:
    raise ValueError(f'expected one {name} for {label}, found {len(refs)}')
uri,=refs
url=urlsplit(uri)
m=re.fullmatch(r'/blobs/([0-9a-f]{64})/(\d+)',url.path)
if url.scheme!='bytestream' or url.netloc!='remote.buildbuddy.io' or url.query or url.fragment or not m:
    raise ValueError('unexpected CAS reference')
digest,n=m.groups(); n=int(n)
if not 0<n<=192<<20:
    raise ValueError('archive exceeds bound')
payload=get('GetFile',{'uri':uri},n)
if len(payload)!=n or hashlib.sha256(payload).hexdigest()!=digest:
    raise ValueError('CAS digest mismatch')
Path(output).write_bytes(payload)
Path(receipt,'materialization.json').write_text(json.dumps({'invocation':invocation,'label':label,'sha256':digest,'bytes':n,'source':'original producer'})+'\n')
