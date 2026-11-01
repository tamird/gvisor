"""Read retained execution records of the two specified builds; never submit work."""
from collections import Counter
import gzip
import json
import os
from pathlib import Path
import re
import urllib.request

INVOCATIONS = ("9e62355a-23f1-4cda-8b49-3371cbf5e9eb","2565546b-e2c3-43d7-b70c-e342086ad2e8")
SOURCE = "14a4e93a872bf6dd49893b6bd9a5f55517167e2b"
key = os.environ["BUILDBUDDY_API_KEY"]
assert key
out = Path(os.environ["RUNNER_TEMP"]) / "original-metadata-evidence"

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def lookup(uid: str) -> list[dict]:
    url = "https://app.buildbuddy.io/rpc/BuildBuddyService/GetExecution"
    request=urllib.request.Request(url,data=json.dumps({"executionLookup":{"invocationId":uid}}).encode(),
           headers={"Content-Type":"application/json","Accept-Encoding":"gzip","x-buildbuddy-api-key":key},method="POST")
    with urllib.request.build_opener(NoRedirect()).open(request,timeout=100) as reply:
        data=reply.read(96*1024*1024+1)
        assert len(data)<=96*1024*1024
        if reply.headers.get('Content-Encoding')=='gzip':
            data=gzip.decompress(data)
    return json.loads(data).get("execution",[])

signals=re.compile(r"RESOURCE_EXHAUSTED|DEADLINE_EXCEEDED|UNAVAILABLE|CANCELLED|CANCELED|ABORTED|INTERNAL|signal: killed|worker lost|out of memory|context canceled|context cancelled|timed out|deadline exceeded|no registered executors|no available executors|executor shutdown|exceeded [a-z ]+limit|connection reset|stream terminated|execution timeout|not found|failed precondition|OOMKilled|Exec format error",re.I)
for uid in INVOCATIONS:
    rows=lookup(uid)
    details=[]
    for row in rows:
        st=row.get('status',{})
        meta=row.get('executedActionMetadata',{})
        stage=row.get('stage')
        if isinstance(stage,dict):
            stage=stage.get('value')
        record=dict(label=row.get('targetLabel'),mnemonic=row.get('actionMnemonic'),
                    digest=row.get('actionDigest',{}).get('hash'),stage=stage,code=st.get('code',0),
                    exit=row.get('exitCode',0),signals=signals.findall(st.get('message','')),
                    times={k:v for k,v in meta.items() if k.endswith('Timestamp')})
        for section,source,allow in [('size',row.get('taskSize',{}),('estimatedMemoryBytes','estimatedMilliCpu')),
                                    ('usage',meta.get('usageStats',{}),('peakMemoryBytes','cpuNanos','memoryBytes'))]:
            record[section]={k:source[k] for k in allow if k in source}
        # A filename/entrypoint/argv can include private data. Only identify CPU
        # configs from Bazel-owned output prefixes, not user-provided strings.
        match=re.search(r'bazel-out/(k8|aarch64|arm64|x86_64)(?:[-_/]|$)',row.get('primaryOutputPath',''))
        if match:
            record['outputCpu']=match.group(1)
        details.append(record)
    payload=json.dumps(dict(source=SOURCE,invocation=uid,rows=details,
                       n=len(rows),status_counts=dict(Counter(f"{r['stage']}/{r['code']}/{r['exit']}" for r in details))),separators=(',',':'))
    assert key not in payload and 'x-buildbuddy-api-key' not in payload.lower()
    (out/(uid+'.json')).write_text(payload+'\n')
    print(uid,len(rows))
