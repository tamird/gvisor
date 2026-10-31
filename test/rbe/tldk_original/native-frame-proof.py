"""Identify the original CFI frames by complete relocation-aware object bytes."""
from pathlib import Path
from io import BytesIO
import hashlib
import json
import os
import subprocess
import urllib.request
from elftools.elf.elffile import ELFFile
from elftools.dwarf.callframe import FDE

d=Path(os.environ['RUNNER_TEMP']);out=d/'receipt';out.mkdir(exist_ok=True)
r={'source':'675d49b1c','stackSource':'https://github.com/tamird/gvisor/actions/runs/41217876785/artifacts/13048353204','captures':{},'inputs':{},'archiveLookup':[]}
def dump():(out/'native-frames.json').write_text(json.dumps(r,indent=2)+'\n')
api='https://app.buildbuddy.io/'
hdr={'Content-Type':'application/json','x-buildbuddy-api-key':os.environ['BUILDBUDDY_API_KEY']}
def req(url,body):return urllib.request.urlopen(urllib.request.Request(api+url,data=json.dumps(body).encode(),headers=hdr),timeout=90)
def cas(h,size,name):
    assert len(h)==64 and 0<size<400000000
    with req('api/v1/GetFile',{'uri':'bytestream://remote.buildbuddy.io/blobs/'+h+'/'+str(size)}) as f:
        b=f.read()
    assert len(b)==size and hashlib.sha256(b).hexdigest()==h
    (d/name).write_bytes(b);r['inputs'][name]={'bytes':size,'sha256':h};dump();return b
def walk(x):
    if isinstance(x,list):
        for v in x:yield from walk(v)
    elif isinstance(x,dict):
        yield x
        for v in x.values():yield from walk(v)
h='7a272c8e0d4556b0879d8e65a6af9fd1865bfc5ef947dcbd7b15ebe8b78ac32a'
b=cas(h,35935770,'original-sentry')
for label in ['@@+_repo_rules+libpluginstack//:libpluginstack','@libpluginstack//:libpluginstack','@@libpluginstack//:libpluginstack','//pkg/sentry/socket/plugin/cgo:cgo']:
    try:
        with req('rpc/BuildBuddyService/GetExecution',{'executionLookup':{'invocationId':'ca7396df-9b3d-415f-9c0c-8899dbaaf9ad','targetLabel':label},'inlineExecuteResponse':True}) as f:x=json.load(f)
    except Exception as exc:
        r['archiveLookup'].append([label,type(exc).__name__]);continue
    candidates=[o for o in walk(x) if str(o.get('path','')).endswith('/libpluginstack.a') and isinstance(o.get('digest'),dict) and 'hash'in o['digest']]
    r['archiveLookup'].append([label,len(x.get('execution',[])),len(candidates)])
    if candidates:
        dig=candidates[0]['digest'];cas(dig['hash'],int(dig['sizeBytes']),'qualified-plugin.a');break
elf=ELFFile(BytesIO(b))
seg=[s for s in elf.iter_segments() if s['p_type']=='PT_LOAD']
def virtual(v):return next(s['p_vaddr']+v-s['p_offset'] for s in seg if s['p_offset']<=v<s['p_offset']+s['p_filesz'])
def off(v):return next(s['p_offset']+v-s['p_vaddr'] for s in seg if s['p_vaddr']<=v<s['p_vaddr']+s['p_filesz'])
fdes=[e for e in elf.get_dwarf_info().EH_CFI_entries() if isinstance(e,FDE)]
ranges={}
for hx in ('e89b3c','e8c587','e8c9bc','e21fd9','e23ec8','e2499c','d465e6','e1ad3a','ee4e2f'):
    v=int(hx,16);pc=virtual(v)
    f=next(z for z in fdes if z['initial_location']<=pc<z['initial_location']+z['address_range'])
    start=off(f['initial_location']);n=f['address_range']
    ranges.setdefault(n,[]).append((hx,start));r['captures'][hx]={'functionStart':hex(start),'CFIBytes':n,'offsetWithin':v-start,'matches':[]}
dump()
def analyze(o,src):
    try:e=ELFFile(BytesIO(o.read_bytes()))
    except Exception:return
    syms=e.get_section_by_name('.symtab')
    if not syms:return
    sections=list(e.iter_sections())
    for x in syms.iter_symbols():
        n=x['st_size'];sid=x['st_shndx']
        if n not in ranges or not isinstance(sid,int) or x['st_info']['type'] not in ('STT_FUNC','STT_GNU_IFUNC','STT_LOOS'):continue
        sec=sections[sid];start=x['st_value'];payload=sec.data()[start:start+n];bad=bytearray(n)
        for rs in sections:
            if rs['sh_type'] not in ('SHT_RELA','SHT_REL') or rs['sh_info']!=sid:continue
            for y in rs.iter_relocations():
                p=y['r_offset']-start
                if -8<p<n:
                    l=max(0,p-3);h=min(n,p+8);bad[l:h]=b'\1'*(h-l)
        cnt=sum(1 for z in bad if not z)
        if cnt<9:continue
        for hx,a in ranges[n]:
            if all(v==b[a+i] for i,v in enumerate(payload) if not bad[i]):
                r['captures'][hx]['matches'].append({'lib':src,'member':o.name,'symbol':x.name,'invariantBytes':cnt})
used=set()
for a in sorted(d.glob('qualified-*.a')):
    raw=a.read_bytes();digest=hashlib.sha256(raw).hexdigest()
    if digest in used:continue
    used.add(digest);r['inputs'][a.name]={'bytes':len(raw),'sha256':digest}
    obj=d/('symbols-'+a.stem);obj.mkdir()
    subprocess.run(['ar','x',a],cwd=obj,check=True)
    for o in obj.iterdir():analyze(o,a.name)
    dump()
r['status']='MATCHED_CALLER' if r['captures']['e21fd9']['matches'] else 'PARTIAL';dump()
