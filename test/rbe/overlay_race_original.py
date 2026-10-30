"""Recover bounded diagnostics from the one completed Linux race test."""
import hashlib,json,os,re,urllib.request
from pathlib import Path
from urllib.parse import urlsplit
inv='8a0ec6b0-804b-4719-8fc8-312ed10e0e03'
label='//pkg/sentry/syscalls/linux:linux_test'
def read(method,payload,limit):
    request=urllib.request.Request("https://app.buildbuddy.io/api/v1/"+method,data=json.dumps(payload).encode(),headers={"x-buildbuddy-api-key":os.environ["BUILDBUDDY_API_KEY"],"Content-Type":"application/json"})
    with urllib.request.urlopen(request,timeout=32) as response:
        raw=response.read(limit+1)
    if len(raw)>limit: raise ValueError("bounded result exceeded")
    return raw
def safe(line):
    if re.search(r'credential|api[_-]?key|authorization|secret|bearer|gh[pousr]_|passwd|\x00',line,re.I):return None
    if re.search(r'(^=== RUN|^--- (?:FAIL|PASS)|^FAIL(?:\t|$)|[Pp]anic:|[Ff]atal error:|WARNING: DATA RACE|[Rr]untime error|Invalid argument|exit status|unexpected|blocked|No such file|Permission denied|FATAL:|ThreadSanitizer|[Ee]rror (?:loading|executing)|error while loading|pthread|ld-linux|GLIBC_|checkptr|^SIG[A-Z]+:|failed to|runtime[.]throw|unexpected signal|fatal signal|gvisor.dev/gvisor/pkg/.+\(0x|No tests were run|test timed out)',line):
        return re.sub(r'0x[0-9a-f]{5,}', '<addr>',line.strip())[:270]
d=json.loads(read('GetAction',{'selector':{'invocation_id':inv,'target_label':label}},2<<20))
report={"invocation":inv,"label":label,"files":[]}
for a in d.get('action',[]):
    for f in a.get('file',[]):
        if f.get('name','').rsplit('/',1)[-1] not in ('test.log','test.xml'): continue
        u=urlsplit(f['uri']);m=re.search(r'/blobs/([a-f0-9]{64})/(\d+)$',u.path)
        if u.scheme!='bytestream' or u.hostname!='remote.buildbuddy.io' or not m:continue
        n=int(m[2])
        if not 0<n<=6<<20:continue
        raw=read('GetFile',{'uri':f['uri']},n)
        if len(raw)!=n or hashlib.sha256(raw).hexdigest()!=m[1]:raise ValueError('action output differs')
        p={'name':f['name'].rsplit('/',1)[-1],'bytes':n,'sha256':m[1]};lines=raw.decode(errors='replace').splitlines()
        if p['name']=='test.log':
            out=[]
            for line in lines:
                item=safe(line)
                if item and item not in out and len(out)<48:out.append(item)
            p['diagnostics']=out
        else:
            import xml.etree.ElementTree as et
            tree=et.fromstring(raw)
            p['suites']=[{'name':x.get('name','')[:70],'tests':x.get('tests'),'errors':x.get('errors'),'failures':x.get('failures')} for x in tree.iter('testsuite')][:10]
        report['files'].append(p)
if not report['files']:
    token="0000"; seen=set(); selected=[]
    while token and token not in seen and len(seen)<55:
        seen.add(token)
        page=json.loads(read('GetLog',{'selector':{'invocation_id':inv},'page_token':token},4<<20))
        raw=page.get('log',{}).get('contents','')
        for line in re.sub(r'\x1b\[[;\d]*[A-Za-z]','',raw).replace('\r','\n').splitlines():
            item=safe(line)
            if item and item not in selected and len(selected)<60:selected.append(item)
        token=page.get('nextPageToken','')
    report['actionCount']=len(d.get('action',[]))
    report['consolePages']=len(seen)
    report['consoleDiagnostics']=selected
    report['fileTypes']=[{'name':f.get('name','').rsplit('/',1)[-1][:50],'scheme':urlsplit(f.get('uri','')).scheme} for a in d.get('action',[]) for f in a.get('file',[])][:15]
print(json.dumps(report));Path(os.environ['RUNNER_TEMP'],'overlay-race-original.json').write_text(json.dumps(report))
