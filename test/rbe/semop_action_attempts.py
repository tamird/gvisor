"""Read retained execution records of the two specified builds; never submit work."""
from collections import Counter
import base64
import gzip
import json
import os
from pathlib import Path
import re
import urllib.request

INVOCATIONS = ("47be79c9-5261-432b-9dd3-5380ba792c96",)
SOURCE = "14a4e93a872bf6dd49893b6bd9a5f55517167e2b"
key = os.environ["BUILDBUDDY_API_KEY"]
assert key
out = Path(os.environ["RUNNER_TEMP"]) / "original-metadata-evidence"

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def rpc(method: str, body: dict) -> dict:
    url = "https://app.buildbuddy.io/rpc/BuildBuddyService/" + method
    request=urllib.request.Request(url,data=json.dumps(body).encode(),
           headers={"Content-Type":"application/json","Accept-Encoding":"gzip","x-buildbuddy-api-key":key},method="POST")
    with urllib.request.build_opener(NoRedirect()).open(request,timeout=100) as reply:
        data=reply.read(96*1024*1024+1)
        assert len(data)<=96*1024*1024
        if reply.headers.get('Content-Encoding')=='gzip':
            data=gzip.decompress(data)
    return json.loads(data)

signals=re.compile(r"RESOURCE_EXHAUSTED|DEADLINE_EXCEEDED|UNAVAILABLE|CANCELLED|CANCELED|ABORTED|INTERNAL|signal: killed|worker lost|out of memory|context canceled|context cancelled|timed out|deadline exceeded|no registered executors|no available executors|executor shutdown|exceeded [a-z ]+limit|connection reset|stream terminated|execution timeout|not found|failed precondition|OOMKilled|Exec format error",re.I)
for uid in INVOCATIONS:
    rows=rpc("GetExecution", {"executionLookup":{"invocationId":uid}}).get("execution",[])
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
    chunks = []
    request = {"invocationId": uid, "minLines": 900}
    visited = set()
    for _ in range(4):
        raw = rpc("GetEventLogChunk", request)
        lines = base64.b64decode(raw.get("buffer", "")).decode(errors="replace").splitlines()
        progress = []
        for line in lines:
            if not re.search(r"Analyzing |Generating |GoStaticAnalysis|action[s]? running|Action[s]? completed|processes:|FAILED|ERROR|CRITICAL PATH", line, re.I):
                continue
            labels = re.findall(r"(?:@{0,2})//[A-Za-z0-9/_.-]+:[A-Za-z0-9/_.-]+", line)
            counts = re.findall(r"\[[0-9,]+ / [0-9,]+\]", line)
            duration = re.findall(r"\b[0-9]+s\b", line)
            strategies = re.findall(r"\b(?:local|remote|linux-sandbox|standalone|worker|processwrapper-sandbox)\b", line)
            operations = re.findall(r"Analyzing|Generating|Testing|FAILED|ERROR|action[s]? running|Critical Path", line, re.I)
            progress.append(dict(labels=labels,counts=counts,durations=duration,strategies=strategies,
                                 operations=operations,signals=signals.findall(line)))
        chunks.append(dict(lines=len(lines),progress=progress[-100:],totalProgress=len(progress)))
        previous=raw.get("previousChunkId") or raw.get("prevChunkId")
        if not previous or previous in visited:
            break
        visited.add(previous)
        request["chunkId"]=previous
    public = json.dumps({"invocation":uid,"chunks":chunks})
    assert key not in public and "x-buildbuddy-api-key" not in public.lower()
    (out/"progress.json").write_text(public + "\n")
