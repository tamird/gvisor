"""Extract the two startup failures from their original undeclared test outputs."""
from hashlib import sha256
from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
from io import BytesIO
from zipfile import ZipFile

inv = "69149f41-f78a-46e7-8dfc-cbbd8c3bcead"
label = "//runsc/container:container_test"
key = os.environ["BUILDBUDDY_API_KEY"]
dest = Path(os.environ["RUNNER_TEMP"]) / "verdict.json"
out = {"invocation": inv, "owners": {}}


def rpc(method, req, raw=False):
    conn = HTTPSConnection("app.buildbuddy.io", timeout=25)
    try:
        conn.request("POST", "/api/v1/"+method, json.dumps(req), {
            "Content-Type":"application/json", "x-buildbuddy-api-key":key})
        with conn.getresponse() as resp:
            if resp.status != 200:
                raise RuntimeError(f"{method}: HTTP {resp.status}")
            data = resp.read(24*1024*1024+1)
            if len(data)>24*1024*1024:
                raise ValueError("oversized response")
            return data if raw else json.loads(data)
    finally:
        conn.close()


def pages(method, req):
    while True:
        d = rpc(method, req)
        yield d
        page = d.get("nextPageToken", d.get("next_page_token"))
        if not page:
            break
        req["page_token"] = page


def diagnostics(name, body):
    if 'TestCheckpointDestinationCapacity' not in name or not name.endswith('.txt'):
        return
    text = body.decode(errors='replace')
    safe = [line.strip()[:1500] for line in text.splitlines()
            if re.search(r'^E[0-9]{4}|^F[0-9]{4}|panic|permission denied|not permitted|fail(?:ed|ure)|cannot|not supported|not found|could not|no such file|invalid argument', line, re.I)
            and not re.search(r'credential|token|password|authorization|api.key|bearer|(?:bes|remote).header', line, re.I)]
    out['components'][name] = safe[-8:]


try:
    out['label'] = label
    out['files'] = []
    out['components'] = {}
    for page in pages('GetAction', {'selector': {'invocation_id': inv, 'target_label': label}}):
        for action in page.get('action', []):
            for f in action.get('file', []):
                name = f.get('name', '')
                out['files'].append(name)
                if not (name.endswith('outputs.zip') or ('TestCheckpointDestinationCapacity' in name and name.endswith('.txt'))):
                    continue
                uri = f.get('uri', '')
                m = re.search(r'/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/(\d+)$', uri)
                if not m or not uri.startswith('bytestream://') or int(m[2]) > 24000000:
                    continue
                data = rpc('GetFile', {'uri': uri}, raw=True)
                if len(data) != int(m[2]) or sha256(data).hexdigest() != m[1]:
                    raise ValueError('CAS checksum mismatch')
                if name.endswith('outputs.zip'):
                    out['zip_sha256'] = m[1]
                    with ZipFile(BytesIO(data)) as z:
                        for member in z.infolist():
                            if member.file_size < 4000000 and 'TestCheckpointDestinationCapacity' in member.filename:
                                diagnostics(member.filename, z.read(member))
                else:
                    diagnostics(name, data)
    out['complete'] = all(any(case in name and lines for name,lines in out['components'].items()) for case in ('full','roomy'))
finally:
    dest.write_text(json.dumps(out,indent=2).replace(key,'[redacted]')+'\n')
if not out['complete']:
    raise SystemExit(1)
