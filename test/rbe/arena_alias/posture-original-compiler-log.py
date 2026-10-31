"""Classify compiler stderr from the retained RBE invocation; do not export the build log."""
import collections
import json
import os
import pathlib
import re
import urllib.request

iid = '77857ede-6a08-4514-b1b3-d0b7cc58a66c'
d = pathlib.Path(os.environ['RUNNER_TEMP']) / 'receipt'
d.mkdir(exist_ok=True)
selector = {'selector': {'invocation_id': iid}}
headers = {'Content-Type': 'application/json', 'x-buildbuddy-api-key': os.environ['BUILDBUDDY_API_KEY']}
tokens = set()
counts = collections.Counter()
pages = 0
with_endpoint = 'https://app.buildbuddy.io/api/v1/GetLog'
while pages < 200:
    req = urllib.request.Request(with_endpoint, data=json.dumps(selector).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=75) as response:
        data = json.load(response)
    log = data['log'].get('contents', '')
    pages += 1
    for s in log.splitlines():
        if 'fatal error: Killed signal terminated program cc1plus' in s:
            counts['fatal error: Killed signal terminated program cc1plus'] += 1
        if 'No space left on device' in s:
            counts['No space left on device'] += 1
        if 'undeclared inclusion(s)' in s:
            counts['undeclared inclusion(s)'] += 1
        if re.search(r'cannot execute .?cc1plus', s):
            counts['cannot execute cc1plus'] += 1
        if 'internal compiler error:' in s:
            counts['internal compiler error'] += 1
        if 'Resource Exhausted:' in s:
            counts['Resource Exhausted'] += 1
    token = data.get('nextPageToken') or data.get('next_page_token')
    if not token:
        break
    if token in tokens:
        raise RuntimeError('repeated log cursor')
    tokens.add(token)
    selector['page_token'] = token
(d/'original-final-compiler.json').write_text(json.dumps({
    'invocation': iid, 'pages': pages, 'complete': not token, 'diagnostics': counts}, indent=2)+'\n')
