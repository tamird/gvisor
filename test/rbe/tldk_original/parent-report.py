import hashlib
import json
import os
from pathlib import Path
import re

p=Path(os.environ['RUNNER_TEMP']);out=p/'receipt'
r={'source':os.environ['PRODUCT'],'declaredInputsControl':'675d49b1c','builder':'gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a','targets':{},'executables':{}}
for f in ('parent-builder.txt','site-kernel.json','diagnosis.json'):
    if (out/f).is_file():r[f]=True
if (p/'parent.bep').exists():
    for line in (p/'parent.bep').open():
        x=json.loads(line)
        if 'started'in x:r['buildInvocation']=x['started']['uuid']
        l=x.get('id',{}).get('targetCompleted',{}).get('label')
        if l in ('//runsc:runsc-plugin-stack','//runsc/cmd/sentry:gvisor_sentry_plugin_stack'):r['targets'][l]=x.get('completed',{}).get('success',False)
        if 'finished'in x:r['buildExit']=x['finished'].get('exitCode')
if r.get('buildExit',{}).get('code',0)==0 and len(r['targets'])==2 and all(r['targets'].values()):
    for f in ('runsc','gvisor-bin/gvisor_sentry_plugin_stack'):
        b=(p/'reused'/f).read_bytes()
        assert b[:6]==b'\x7fELF\x02\x01' and int.from_bytes(b[18:20],'little')==62
        r['executables'][f]=hashlib.sha256(b).hexdigest()
else:
    log=p/'parent.log'
    if log.exists():
        r['buildErrors']=[s.strip()[:500] for s in log.read_text(errors='replace').splitlines() if any(t in s for t in ('fatal:','error:','ERROR:','No such file','not found','No space left')) and not re.search(r'(?i)secret|credential|token|api.key|header|authorization',s)][-15:]
(out/'parent-result.json').write_text(json.dumps(r,indent=2)+'\n')
