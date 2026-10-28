"""Verify one stock FileNode and tar member on the same original package action."""
import ast
import datetime
import hashlib
import http.client
import io
import json
from pathlib import Path
import posixpath
import tarfile

here=Path(__file__).resolve().parent
original=here/'metadata.py'
source='01446201fd8e758267f4e1abd9727926655110b7'
known_input=['d9014c4624844aa5bac314773d6b689ad467fa4e1d1a50a1b8a99d5a95f72ff5',14]
label='//tests/tar:test-tar-preserve_mode-True'

def verify(key, receipt):
    archive=here/'qualification.tar.gz';submitted=json.loads((here/'submitted.json').read_bytes())
    out={'source':source,'native_parent':submitted['invocation_id'],'lookups':[],'status':'INCOMPLETE','archive':receipt.get('artifact')}
    try:
        assert receipt['sources']==receipt['expected_sources']==[source] and receipt['collection_status']=='COMPLETE' and receipt['native_invocation']['invocationId']==submitted['invocation_id']
        assert hashlib.sha256(archive.read_bytes()).hexdigest()==receipt['artifact']['sha256']
        with tarfile.open(archive) as t:
            actions=json.load(t.extractfile('supplier-tests-image-execution.json'))['image_actions']
            rows=[json.loads(x) for x in t.extractfile('invocation-supplier-tests/events.jsonl')]
            [out['child']]=[x['started']['uuid'] for x in rows if 'started' in x.get('id',{})]
        [a]=[a for a in actions if a['targetLabel']==label and a['mnemonic']=='PackageTar' and not a['cacheHit'] and a['runner']=='remote' and a['exitCode']==0]
        names={'fields','one','ident','sha','get','file_at'}
        defs=[z for z in ast.parse(original.read_bytes()).body if isinstance(z,ast.FunctionDef) and z.name in names]
        assert {z.name for z in defs}==names
        scope=dict(globals(),key=key,out=out,seen={},total=0)
        exec(compile(ast.Module(body=defs,type_ignores=[]),'verified-original-file-reader','exec'),scope)
        get=scope['get'];one=scope['one'];ident=scope['ident']
        action=[a['digest']['hash'],int(a['digest']['sizeBytes'])]
        ab=get(action);root=ident(one(ab,2));command=ident(one(ab,1));cmd=get(command)
        assert [v.decode() for k,v in scope['fields'](cmd) if k==1]==a['commandArgs']
        node=scope['file_at'](root,'tests/testdata/hello.txt');[v]=a['actualOutputs']
        tar_digest=[v['digest']['hash'],int(v['digest']['sizeBytes'])]
        with tarfile.open(fileobj=io.BytesIO(get(tar_digest,2**20))) as t:
            [m]=t.getmembers();payload=t.extractfile(m).read()
        assert m.name=='hello.txt' and [hashlib.sha256(payload).hexdigest(),len(payload)]==node['digest']
        assert node['digest']==known_input
        out.update(status='COMPLETE',action=action,input_root=root,command=command,argv=a['commandArgs'],hello_input=node,tar=tar_digest,member={'name':m.name,'mode':oct(m.mode),'bytes':len(payload),'sha256':hashlib.sha256(payload).hexdigest()},candidate_mode='0o755',same_input_mode=(node['is_executable'],node['unix_mode'])==(True,None),same_header_mode=oct(m.mode)=='0o755',metadata_bytes=scope['total'])
    except Exception as e:
        out['error']=type(e).__name__+': '+str(e)[:280]
    serialized=json.dumps(out,indent=2,sort_keys=True)+'\n';assert key not in serialized
    (here/'original-action-mode.json').write_text(serialized);print('Stock input mode:',out['status'],out.get('error',''),flush=True)
    return out['status']=='COMPLETE'
