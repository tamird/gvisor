"""Publish only fields derived from authenticated original test reports."""
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import sys
import tarfile
from uuid import UUID
import xml.etree.ElementTree as ET
def sha(b): return hashlib.sha256(b).hexdigest()
def report(task):
    r=json.loads((task/"receipt.json").read_bytes())
    native=r["native_invocation"]
    out={"source":SOURCE,"request":REQUEST,"invocation":str(UUID(native["invocationId"])),"parent":r["status"],"producer":r.get("producer_status"),"collection":r["collection_status"],"phases":{}}
    if r.get("consumer_verified_at") and native.get("durationUsec"):
        finish=datetime.fromtimestamp((int(native["createdAtUsec"])+int(native["durationUsec"]))/1e6,timezone.utc)
        verified=datetime.fromisoformat(r["consumer_verified_at"].replace("Z","+00:00"))
        out.update(finished=finish.isoformat(),verified=verified.isoformat(),delay_s=(verified-finish).total_seconds())
    f=task/"qualification.tar.gz"
    if not f.is_file():return out
    raw=f.read_bytes();out["archive"]={"sha256":sha(raw),"bytes":len(raw)}
    if "artifact" in r:
        assert out["archive"]=={k:r["artifact"][k] for k in out["archive"]}
        out["canonical_archive"]=True
    else:
        assert r["status"]!="PASS"
        out["canonical_archive"]=False
    with tarfile.open(f) as bundle:
        members={m.name.removeprefix('./'):m for m in bundle.getmembers() if m.isfile()}
        def read(name):
            hits=[m for k,m in members.items() if k==name or k.endswith('/'+name)]
            if not hits:return None
            assert len(hits)==1 and hits[0].size<=256*1024*1024
            return bundle.extractfile(hits[0]).read()
        initial=read('invocation-00-buildifier/native-exit')
        if initial is not None:out['buildifier_exit']=int(initial)
        result=read('sidecar-review.json')
        intermediate=json.loads(result) if result else {}
        for compiler in ('gnu','clang'):
            for arm in ('x86_64','aarch64','tests'):
                phase=compiler+'-'+arm; inp=intermediate.get(phase,{})
                row=out['phases'][phase]={}
                events=read('invocation-'+phase+'/events.jsonl')
                if events:
                    for line in events.splitlines():
                        e=json.loads(line)
                        if 'started' in e:row['invocation']=str(UUID(e['started']['uuid']))
                        if 'finished' in e:row['native_exit']=int(e['finished']['exitCode'].get('code',0))
                for k in ('verified','allocator_query'):
                    if isinstance(inp.get(k),bool):row[k]=inp[k]
                if inp.get('native') in ('PASS','FAIL','INCOMPLETE'):row['native']=inp['native']
                if inp.get('diagnostic'):
                    row['diagnostic_lines']=[int(n) for n in re.findall(r'image_workload[.]py", line ([0-9]+)',inp['diagnostic'])][:8]
                for group in ('outputs','results'):
                    for label, item in inp.get(group,{}).items():
                        assert label in OWNERS;safe={}
                        for k in ('machine','entry','bytes','cases'):
                            if k in item:assert type(item[k]) is int and 0<=item[k]<2**63;safe[k]=item[k]
                        for k in ('source','sha256','xml','action'):
                            if k in item:assert re.fullmatch('[a-f0-9]{64}',item[k]);safe[k]=item[k]
                        for k in ('compile','link'):
                            if k in item:
                                act=item[k];assert re.fullmatch('[a-f0-9]{64}',act['action']) and type(act['cache_hit']) is bool
                                safe[k]={'action':act['action'],'cache_hit':act['cache_hit']}
                        row.setdefault(group,{})[label]=safe
    return out

SOURCE='4b4d38fbf0ccfa1e4a808e9f3715edab3f1a6330'
REQUEST='43914609b167ccd12b25fe32b25e7e4901217f0d34fa094ab48ee18586ab5057'
OWNERS=['//runsc/fdparking:fdparking_binary', '//runsc/prewarmer:prewarmer_binary', '//runsc/procumounter:procumounter_binary', '//runsc/fdparking:fdparking_test', '//runsc/prewarmer:prewarmer_single_page_test', '//runsc/procumounter:procumounter_test']
class Quiet:
    def write(self,s):
        if re.fullmatch(r"https://app\.buildbuddy\.io/invocation/[0-9a-f-]{36}",s):
            print(s,file=sys.__stdout__,flush=True)
        return len(s)
    def flush(self):pass

def collect(key):
    root=Path(os.environ["RUNNER_TEMP"])/"sidecar-review"
    task=root/"task"
    public={"source":SOURCE,"request":REQUEST,"stage":"submit"}
    rc=1
    try:
        assert key
        sender=runpy.run_path(str(task/"submit.py"))
        with redirect_stdout(Quiet()),redirect_stderr(Quiet()):
            rc=sender["main"](lambda _:key)
        key=None
        public["stage"]="reconcile"
        public=report(task)
    except BaseException as exc:
        rc=1
        public["projection_error"]=type(exc).__name__ if type(exc).__name__ in ("KeyError","ValueError","AssertionError","TypeError","IndexError","FileNotFoundError","StopIteration") else "OTHER"
        tb=exc.__traceback__;lines=[]
        while tb:
            if Path(tb.tb_frame.f_code.co_filename).name=="receipt.py":lines.append(tb.tb_lineno)
            tb=tb.tb_next
        public["projection_lines"]=lines[:8]
    finally:
        key=None
        marker=task/"submission.json"
        if marker.is_file():public["invocation"]=str(UUID(json.loads(marker.read_bytes())["invocationId"]))
        (root/"verdict.json").write_text(json.dumps(public,indent=2)+"\n")
    raise SystemExit(rc)
