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
r={'source':'9e3e72d73','initialEvidence':'41217449718/13049805793','captures':{},'inputs':{}}
def dump():(out/'native-frames.json').write_text(json.dumps(r,indent=2)+'\n')
import urllib.request
q=urllib.request.Request('https://app.buildbuddy.io/api/v1/GetFile',data=json.dumps({'uri':'bytestream://remote.buildbuddy.io/blobs/48130c707df9a85d4da8939a64d67af570cc15b3f3542c8ede0deeb4cc0848dd/35952322'}).encode(),headers={'Content-Type':'application/json','x-buildbuddy-api-key':os.environ['BUILDBUDDY_API_KEY']})
with urllib.request.urlopen(q,timeout=100) as z:b=z.read()
assert hashlib.sha256(b).hexdigest()=='48130c707df9a85d4da8939a64d67af570cc15b3f3542c8ede0deeb4cc0848dd'
r['inputs']['parent-sentry']={'sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b)}
obs=json.loads((out/'denied-openat.json').read_text())
cases=json.loads((out/'diagnosis.json').read_text())
hits=[x for x in obs['fatal'] if x['registerSyscall']==257 and x['syscallSite'] and x['syscallSite']['elfOffset']=='0xe88bba' and x['pathname']=='/sys/devices/system/cpu/online' and int(str(x['flags']),0)==524288 and any(z['pid'] in (str(x['pid']),str(x['tid'])) for z in obs['audit'])]
r['observedSamePathAndOffset']=len(hits);r['actualCases']=cases['bounds'];dump()
if not hits: raise SystemExit('No old-image path captured in the bounded workload; no positive source attribution.')
frames=hits[0]['cfiFrames']
elf=ELFFile(BytesIO(b))
seg=[s for s in elf.iter_segments() if s['p_type']=='PT_LOAD']
def virtual(v):return next(s['p_vaddr']+v-s['p_offset'] for s in seg if s['p_offset']<=v<s['p_offset']+s['p_filesz'])
def off(v):return next(s['p_offset']+v-s['p_vaddr'] for s in seg if s['p_vaddr']<=v<s['p_vaddr']+s['p_filesz'])
fdes=[e for e in elf.get_dwarf_info().EH_CFI_entries() if isinstance(e,FDE)]
ranges={}
for hx in [x['elfOffset'][2:] for x in frames]:
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
actual={m['symbol'] for v in r['captures'].values() for m in v['matches']}
chain={'__libc_free','tcache_init.part.0','arena_get2.part.0','__get_nprocs','get_nprocs_cpu_online','__open64_nocancel'}
r['sourceCapture']='https://github.com/tamird/gvisor/actions/runs/41220392800/artifacts/13049931379';r['missingExpectedFrames']=sorted(chain-actual);r['status']='MATCHED_CALLER' if not (chain-actual) else 'PARTIAL';dump()
