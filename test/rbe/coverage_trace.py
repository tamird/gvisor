"""Recover only Starlark diagnostics from two known original BuildBuddy builds."""
import json
import os
import re
import urllib.request
from pathlib import Path

dest=Path(os.environ['RUNNER_TEMP'])/'package-trace'
dest.mkdir()
ids={'amd64':'f36292ac-490a-4601-9a24-5892c7565c2a','arm64':'070e2668-481d-463a-8144-2d05feb9c629'}
key=os.environ['BUILDBUDDY_API_KEY']
for arch,i in ids.items():
    args={'selector':{'invocation_id':i}}
    out=[]
    while True:
        req=urllib.request.Request('https://app.buildbuddy.io/api/v1/GetLog',data=json.dumps(args).encode(),headers={'Content-Type':'application/json','x-buildbuddy-api-key':key})
        with urllib.request.urlopen(req,timeout=30) as r: data=json.load(r)
        for line in data.get('log',{}).get('contents','').splitlines():
            line=re.sub(r'\x1b\[[;\d]*m','',line).strip()
            if not (line.startswith(('Error ', 'Error:', 'error:', 'ERROR:')) or re.match(r'File ".+\.(?:bzl|bazel)", line ',line) or ('Error in ' in line and 'coverage' in line)):continue
            if any(v in line.lower() for v in ['key=','header','bearer ','authorization','https://']) or key in line:continue
            if line not in out:out.append(line[:800])
        token=data.get('nextPageToken') or data.get('next_page_token')
        if not token:break
        args['page_token']=token
    (dest/(arch+'.json')).write_text(json.dumps({'id':i,'trace':out},indent=2)+'\n')
    if not any('recursive' in s or s.startswith(('Error in','Error:')) for s in out):
        print('No terminal diagnostic recognized for '+arch)
