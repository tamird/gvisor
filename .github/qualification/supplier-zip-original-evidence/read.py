"""Read one immutable original archive and only its three package action inputs."""
import gzip,hashlib,http.client,io,json,os,posixpath,re,tarfile,zipfile,struct,sys
from pathlib import Path
from uuid import UUID
sys.path.insert(0,str(Path(__file__).resolve().parent))
from execution_records import execution_records
import xml.etree.ElementTree as ET
ARCHIVE=["59722fdc6a35483d07a9853515cb314601c6c421ce87d3d104c13c80377a534b",76275]
SOURCE="9107384786056cdc05c9ad869bc07a577c66e746";CHILD="211053ef-e8f1-4653-acb7-710a5065ef37";PARENT="a288512e-7fcd-4126-adce-232ddb22bfa9"
WRITERS={'deb/make_deb.py': '8b1aa6391405607b67eee11192412f95694027c7558d5eb8b0c6f667d0694046', 'tar/build_tar.py': '8f4900918caadae87b72ab60adb4ce0746e3908704e91dd1fca98162414c9690'}
EXC="a08877cfcf1bfb9912462046f88e93e6b6035396e510dde9a92b63305356ac65"
ROOT=Path(os.environ["RUNNER_TEMP"])/"supplier-zip-original-evidence";META=Path(__file__).with_name("metadata.py")
assert hashlib.sha256(META.read_bytes()).hexdigest()=="7c211c8878c04269feb91a684480fef2e66f940f9e5785310055056f19428cb3"
def hashes(b):return hashlib.sha256(b).hexdigest()
def getpair(x):return [x['hash'],int(x['sizeBytes'])]
def safeexc(s):
    typ=s.partition(':')[0]
    kinds=('TypeError','NameError','KeyError','ValueError','AttributeError','ModuleNotFoundError','FileNotFoundError','RuntimeError','IndexError','ImportError','AssertionError','OSError','BadZipFile','struct.error','error')
    out={'sha256':hashes(s.encode()),'type':typ if typ in kinds else 'Other'}
    for pat in (r"NameError: name '([a-zA-Z_]\w{0,55})' is not defined",r"TypeError: (string indices must be integers(?:, not 'str')?|'[a-zA-Z_]{1,40}' object is not (?:callable|subscriptable|iterable)|cannot unpack non-iterable [a-zA-Z_]+ object)",r"ModuleNotFoundError: No module named '([a-zA-Z_.]{1,64})'",r"AttributeError: '(?:str|list|dict|module|PosixPath|NoneType|int|datetime)' object has no attribute '([a-zA-Z_]{1,50})'",r"KeyError: '([a-zA-Z_]{1,50})'",r"ImportError: cannot import name '([a-zA-Z_]{1,50})' from '([a-zA-Z_.]{1,70})'.{0,500}"):
        m=re.fullmatch(pat,s)
        if m:out['matched_parts']=list(m.groups());break
    return out
def main(key):
    public={'source':SOURCE,'parent':PARENT,'child':CHILD,'archive':ARCHIVE,'status':'INCOMPLETE','owners':{},'products':{}}
    ctx={'key':key,'out':{'lookups':[]},'seen':{},'total':0,'hashlib':hashlib,'http':__import__('http'),'json':json,'posixpath':posixpath}
    exec(compile(META.read_bytes(),'original fixed-origin CAS reader','exec'),ctx)
    try:
        data=ctx['get'](ARCHIVE);assert [hashes(data),len(data)]==ARCHIVE
        with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as tf:
            reg={m.name:m for m in tf.getmembers() if m.isfile()};assert len(reg)<256 and sum(m.size for m in reg.values())<15_000_000
            def read(n):return tf.extractfile(reg[n]).read()
            def item(n):return json.loads(read(n))
            z=item('supplier-zip.json');assert z['source']==SOURCE and z['status']=='INCOMPLETE' and z['error'] and hashes(z['error'].encode())==EXC
            public['exception']=safeexc(z['error']);report=item('verdict.json');assert z['tree']=='6083f0c21f92d633b5c86ea773bd7a55ac8bcce1' and report['sources']==report['expected_sources']==[SOURCE]
            public['trace']=[]
            for n,m in reg.items():
                if m.size>2_000_000:continue
                b=read(n)
                if EXC.encode() in b:continue
                for _,line,name in re.findall(rb'File "[^"\n]*(image_workload|static_fixture|image_qualification)\.py", line (\d{1,4}), in ([a-zA-Z_]\w{0,45})',b):
                    frame=[int(line),name.decode()]
                    if frame not in public['trace'] and len(public['trace'])<16:public['trace'].append(frame)
            pre='invocation-supplier-tests/'
            eroot=item(pre+'events.jsonl.manifest.json');ebytes=read(pre+'events.jsonl');assert [hashes(ebytes),len(ebytes)]==[eroot['events']['sha256'],eroot['events']['bytes']]
            events=[json.loads(l) for l in ebytes.splitlines()];assert [e['started']['uuid'] for e in events if 'started' in e.get('id',{})]==[CHILD]
            [summary_name]=[n for n in reg if n.endswith('supplier-tests-image-execution.json')]
            summary=item(summary_name);actions=summary['image_actions']
            public['exception']['matched_reason']='Remote image action omitted action/output identity' if hashes(b'RuntimeError: Remote image action omitted action/output identity')==EXC else None
            public['recorder']=dict(status=summary['status'],uncompressed=summary['uncompressed'],missing_output_identities=[{'mnemonic':a['mnemonic'] if re.fullmatch('[A-Za-z0-9_]{1,48}',a['mnemonic']) else 'other','outputs':len(a['actualOutputs']),'missing_digest':sum(not d.get('digest',{}).get('hash') for d in a['actualOutputs'])} for a in actions if a['runner']=='remote' and not a.get('cacheHit') and (not a['digest']['hash'] or not a['actualOutputs'] or any(not d.get('digest',{}).get('hash') for d in a['actualOutputs']))][:8])
            cfg=json.loads(Path(__file__).with_name("plan.json").read_bytes())['config']
            for e in events:
                if 'testResult' not in e.get('id',{}):continue
                ident=e['id']['testResult'];lab=ident['label'];assert lab in cfg['targets'] and not e['testResult'].get('cachedLocally') and [ident[k] for k in ('run','attempt','shard')]==[1,1,1]
                reps={o['name']:o for o in eroot['reports'] if o['identity']==ident and o['name'] in ('test.xml','test.log')};assert set(reps)=={'test.xml','test.log'}
                b={n:read(pre+o['path']) for n,o in reps.items()};assert all(hashes(b[n])==o['sha256'] and len(b[n])==o['bytes'] for n,o in reps.items())
                test=[a for a in actions if a['targetLabel']==lab and a['mnemonic']=='TestRunner']
                [phys]=[a for a in test if any(x.endswith('/test-setup.sh') for x in a['commandArgs'])]
                assert phys['runner']=='remote' and not phys.get('cacheHit') and any(any(d['path'].endswith('/test.xml') and getpair(d['digest'])==[hashes(b['test.xml']),len(b['test.xml'])] for d in a['actualOutputs']) for a in test)
                [count]=re.findall(rb'(?m)^Ran (\d+) tests? in ',b['test.log']);xml=ET.fromstring(b['test.xml'])
                fail=len(xml.findall('.//failure'))+len(xml.findall('.//error'));status=e['testResult']['status'];assert status in ('PASSED','FAILED') and not xml.findall('.//skipped')
                public["owners"][lab]={'status':status,'cases':int(count),'failures':fail,'known_mode_only':fail==1 and b'493 != 420 : file mode not preserved' in b['test.log'] and b'FAILED (failures=1)' in b['test.log'],'action':phys['digest']['hash'],'primary':{n:[o['sha256'],o['bytes']] for n,o in reps.items()}}
            assert set(public['owners'])==set(cfg['targets'])
            extra=[]
            for label,ex in cfg['gold'].items():
                [a]=[a for a in actions if a['targetLabel']==label and a['mnemonic'] in ('MakeDeb','PackageTar')];assert a['runner']=='remote' and not a.get('cacheHit') and a['exitCode']==0
                args=a['commandArgs'];row=public['products'][label]={'action':getpair(a['digest']),'cmd_record':a['full_record_sha256'],'input_count':a['input_count'],'outputs':{},'zip_argv':args[1].endswith('.zip')}
                for d in a['actualOutputs']:
                    [k]=[k for k in ex if d['path'].endswith('.'+k)];p=getpair(d['digest']);row['outputs'][k]={'digest':p,'stock':ex[k]==p}
                row['metrics']={k:v for k,v in a.get('metrics',{}).items() if k in ('totalTime','executionWallTime','inputFetchTime','uploadTime') and isinstance(v,str) and re.fullmatch(r'[0-9]+(\.[0-9]{1,9})?s',v)}
                assert set(row['outputs'])==set(ex);extra.append((label,row,args))
            full=[]
            try:
                rr=summary['execution_log'];raw=ctx['get']([rr['sha256'],rr['bytes']],maxsize=64_000_000);public['compressed_audit']=[rr['sha256'],rr['bytes']]
                hh=hashlib.sha256();total=0
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f, io.TextIOWrapper(f,encoding='utf8',newline='') as stream:
                    for st,action in execution_records(stream):
                        bb=st.encode();hh.update(bb);total+=len(bb);assert total<2_147_483_648
                        if action and action['mnemonic'] in ('PythonZipper','MakeDeb','PackageTar'):full.append(action)
                assert [hh.hexdigest(),total]==[summary['uncompressed']['sha256'],summary['uncompressed']['bytes']]
                public['raw_verified']=True
            except Exception as e:public['raw_read_error']=safeexc(type(e).__name__+': '+str(e))
            for label,row,args in extra:
                try:
                    fields,one,ident,find=[ctx[n] for n in ('fields','one','ident','file_at')]
                    act=ctx['get'](row['action']);cd=ident(one(act,1));rd=ident(one(act,2));cmd=ctx['get'](cd);assert [v.decode() for n,v in fields(cmd) if n==1]==args
                    row['command']=cd;row['root']=rd
                    ent=find(rd,args[0]);row['interpreter']=ent['digest'];row['executable_mode']=ent['is_executable'];b=ctx['get'](ent['digest'],maxsize=220_000_000)
                    assert b[:6]==b'\x7fELF\x02\x01' and int.from_bytes(b[18:20],'little')==62
                    p=struct.unpack_from('<Q',b,32)[0];size,count=struct.unpack_from('<HH',b,54);row['dynamic_loader']=any(struct.unpack_from('<I',b,p+i*size)[0]==3 for i in range(count))
                    if row['zip_argv']:
                        ent=find(rd,args[1]);row['zip']=ent['digest']
                        if public.get('raw_verified'):
                            prod=[a for a in full if a['mnemonic']=='PythonZipper' and any(d['path']==args[1] and getpair(d['digest'])==row['zip'] for d in a['actualOutputs'])]
                            pk=[]
                            for a in prod:
                                pk.append({'action':getpair(a['digest']),'remote':a['runner']=='remote','cached':a.get('cacheHit') is True,'exit':a['exitCode'],'metrics':{k:v for k,v in a.get('metrics',{}).items() if k in ('totalTime','executionWallTime','inputFetchTime','uploadTime') and isinstance(v,str) and re.fullmatch(r'[0-9]+(\.[0-9]{1,9})?s',v)}})
                            row['zip_producers']=pk
                        b=ctx['get'](ent['digest'],maxsize=240_000_000)
                        with zipfile.ZipFile(io.BytesIO(b)) as zf:
                            items=zf.infolist();assert len(items)==len({i.filename for i in items}) and '__main__.py' in zf.namelist()
                            suffix='/pkg/private/'+('deb/make_deb.py' if label=='//tests/deb:test_deb' else 'tar/build_tar.py')
                            [target]=[n for n in zf.namelist() if n.endswith(suffix)]
                            row['writer_source_matches']=hashes(zf.read(target))==WRITERS[suffix[len('/pkg/private/'):]]
                            row['zip_metrics']={'bootstrap':hashes(zf.read('__main__.py')),'writer':hashes(zf.read(target)),'entries':len(items),'expanded_bytes':sum(i.file_size for i in items),'compressed_member_bytes':sum(i.compress_size for i in items)}
                except Exception as e:row['re_read_error']=safeexc(type(e).__name__+': '+str(e))
            public['status']='COMPLETE'
    except Exception as e:public['read_error']=safeexc(type(e).__name__+': '+str(e))
    finally:
        public['read_count']=len(ctx['out']['lookups']);public['read_bytes']=ctx['total']
        (ROOT/'verdict.json').write_text(json.dumps(public,indent=2)+'\n')
    return int(public['status']!='COMPLETE')
