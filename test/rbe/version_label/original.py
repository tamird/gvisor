"""Verify the original release outputs without invoking Bazel or running a build."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from http.client import HTTPSConnection
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
HOST = 'app.buildbuddy.io'
SECRET = os.environ.pop('BUILDBUDDY_API_KEY')
SOURCE = '3a9e0219f76441cdbc08f1c1722b654085f6c765'
OWNER = '//test/rbe/version_label:restore'
FILES = {'//debian:debian': 'runsc.deb', '//debian:gvisor-release-tar-bz2': 'gvisor.tar.bz2',
         '//debian:gvisor-release-tar-zstd': 'gvisor.tar.zstd'}
DIGESTS = {'test.log': ('0b0bc0aeeb0eba15509c2ee8c8709664d3173a556211f6792f4b2f2be5aabf56', 991),
           'test.xml': ('80853957085a6d87c994a791369b3c15bdca78a7c1f55da8b4ccb1f01613c8aa', 1388)}
ROOT = Path(os.environ['RUNNER_TEMP']) / 'version-original'
OUT = ROOT / 'receipt'
ledger = {'source': SOURCE, 'original_actions': 40844444929, 'rows': []}


def request(path, body, maximum, dest=None):
    allowed = {'/rpc/BuildBuddyService/SearchInvocation', '/rpc/BuildBuddyService/GetTarget',
               '/api/v1/GetInvocation', '/api/v1/GetAction', '/api/v1/GetFile'}
    assert path in allowed
    con = HTTPSConnection(HOST,timeout=120)
    try:
        con.request('POST',path,json.dumps(body),{'Content-Type':'application/json','x-buildbuddy-api-key':SECRET})
        with con.getresponse() as response:
            if response.status != 200:
                raise RuntimeError(f'{path}: HTTP {response.status}')
            if dest is None:
                data = response.read(maximum + 1)
                assert len(data) <= maximum
                return json.loads(data)
            digest = hashlib.sha256()
            size = 0
            with dest.open('wb') as stream:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    assert size <= maximum
                    digest.update(chunk)
                    stream.write(chunk)
            return (digest.hexdigest(),size)
    finally:
        con.close()


def search(**query):
    query.update(updatedAfter='2026-10-29T08:46:07Z',updatedBefore='2026-10-29T09:18:46Z')
    result = request('/rpc/BuildBuddyService/SearchInvocation', {'query':query,'count':100}, 8 << 20)
    assert not result.get('nextPageToken')
    return result.get('invocation',[])


def metadata(iid):
    assert re.fullmatch(r'[0-9a-f-]{36}', iid)
    [row] = request('/api/v1/GetInvocation',{'selector':{'invocation_id':iid}},1 << 20)['invocation']
    assert row['id']['invocationId'] == iid
    return row


def target(iid, owner):
    response = request('/rpc/BuildBuddyService/GetTarget',{'invocationId':iid,'targetLabel':owner},16 << 20)
    groups=response.get('targetGroups',response.get('targetGroup',[]))
    values=[t for group in groups for t in group.get('targets',[]) if t['metadata']['label']==owner]
    return [f for t in values for f in t.get('files',[])]


def action(iid, owner):
    result=request('/api/v1/GetAction',{'selector':{'invocation_id':iid,'target_label':owner}},8 << 20)
    return [f for a in result.get('action',[]) for f in a.get('file',[])]


def identity(uri):
    url=urlsplit(uri)
    assert url.scheme=='bytestream' and url.hostname=='remote.buildbuddy.io'
    match=re.fullmatch(r'(?:/[^.][^/]*)*/blobs/([a-f0-9]{64})/([0-9]+)',url.path)
    assert match
    return match[1],int(match[2])


def main():
    try:
        anchors = search(pattern=OWNER,command='test',commitSha=SOURCE)
        if not anchors:
            anchors = search(pattern=OWNER,command='test')
    except RuntimeError:
        anchors = []
    if not anchors:
        data = request('/api/v1/GetInvocation',{'selector':{'commit_sha':SOURCE}},4<<20)
        anchors=[dict(r,invocationId=r['id']['invocationId']) for r in data.get('invocation',[]) if r.get('pattern')==OWNER and r.get('command')=='test']
    assert len(anchors)==1, f'{len(anchors)} restore invocations'
    iid=anchors[0]['invocationId']
    anchor=metadata(iid)
    assert anchor.get('success') and anchor['pattern']==OWNER
    assert not anchor.get('commitSha') or anchor['commitSha']==SOURCE
    actual={f['name']:identity(f['uri']) for f in action(iid,OWNER) if f['name'] in DIGESTS}
    assert set(actual)==set(DIGESTS) and all(actual[name]==DIGESTS[name] for name in actual), actual
    ledger['anchor']={'id':iid,'files':actual}
    assert anchor.get('host')
    for label, filename in FILES.items():
        rows=search(host=anchor['host'],pattern=label,command='build')
        assert len(rows)==2, f'{label} matched {len(rows)} builds'
        ordered=sorted((metadata(row['invocationId']) for row in rows),key=lambda r:int(r['createdAtUsec']))
        for arch,r in zip(('x86_64','aarch64'),ordered):
            iid=r['id']['invocationId']
            assert r['pattern']==label and r['host']==anchor['host'] and r['success']
            # A group may list the same URI twice (top-level file and artifact list).
            files=target(iid,label)+action(iid,label)
            uris={f['uri'] for f in files if Path(f.get('name','')).name==filename}
            assert len(uris)==1, (label,arch,len(uris),sorted({f.get('name','') for f in files})[:8])
            [uri]=uris; digest,n=identity(uri)
            assert 0<n<350_000_000
            dest=ROOT/arch/filename
            dest.parent.mkdir(exist_ok=True)
            assert request('/api/v1/GetFile',{'uri':uri},n,dest)==(digest,n)
            ledger['rows'].append({'arch':arch,'source_owner':label,'id':iid,'filename':filename,'sha256':digest,'bytes':n,
                                   'created':r['createdAtUsec'],'updated':r['updatedAtUsec']})
    assert len(ledger['rows'])==6
    proc=subprocess.run([sys.executable,str(HERE/'packages.py'),str(ROOT),'release-20261029.0',str(OUT/'packages.json')],capture_output=True,text=True)
    if proc.returncode:
        # Only this fixed local parser's final exception/frame is relevant.
        ledger['package_error']=proc.stderr.splitlines()[-4:]
        for a in ('x86_64','aarch64'):
            p=ROOT/a/'runsc.deb'
            ledger.setdefault('control',{})[a]={k:subprocess.check_output(['dpkg-deb','-f',str(p),k],text=True).strip()
                                             for k in ('Package','Version','Architecture')}
        raise ValueError('package comparison failed')


if __name__=='__main__':
    OUT.mkdir(parents=True)
    try:
        main()
        ledger['status']='PASS'
    except Exception as exc:
        ledger['status']='FAIL'
        ledger['reason']=f'{type(exc).__name__}: {exc}'[:900]
        raise
    finally:
        (OUT/'original.json').write_text(json.dumps(ledger,indent=2)+'\n')
