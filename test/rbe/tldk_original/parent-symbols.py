"""Match the original Sentry's actual machine code to the builder's GNU objects."""
import hashlib
from io import BytesIO
import json
from pathlib import Path
import os
import struct
import subprocess
from elftools.elf.elffile import ELFFile

p=Path(os.environ['RUNNER_TEMP'])
b=(p/'reused/gvisor-bin/gvisor_sentry_plugin_stack').read_bytes()
assert hashlib.sha256(b).hexdigest()=='7a272c8e0d4556b0879d8e65a6af9fd1865bfc5ef947dcbd7b15ebe8b78ac32a'
elf=ELFFile(BytesIO(b))
text=elf.get_section_by_name('.text')
bias=text['sh_addr']-text['sh_offset']
first=text['sh_offset'];limit=first+text['sh_size']
capture={'syscall':0xe89b3a,'possibleReturn':0xe8c587}
report={'originalSha256':hashlib.sha256(b).hexdigest(),'captured':{},'archives':[]}
o=p/'receipt/parent-symbols.json'
def dump():o.write_text(json.dumps(report,indent=2)+'\n')
def run(*args):return subprocess.check_output(args,stderr=subprocess.DEVNULL)
def fingerprint(tmp):
    e=ELFFile(BytesIO(tmp));table=e.get_section_by_name('.symtab')
    if table is None:return
    for fn in table.iter_symbols():
        i=fn['st_shndx'];sz=fn['st_size']
        if fn['st_info']['type']!='STT_FUNC' or not isinstance(i,int) or not 11<=sz<=200000:continue
        sec=e.get_section(i);pos=fn['st_value']
        if not sec['sh_flags']&4:continue
        s=sec.data()[pos:pos+sz]
        if len(s)!=sz:continue
        forbidden=set()
        for rel in e.iter_sections():
            if rel['sh_type'] not in ('SHT_REL','SHT_RELA') or rel['sh_info']!=i:continue
            for r in rel.iter_relocations():
                x=r['r_offset']-pos
                if -8<x<sz:
                    # Cover the relocation and instructions relaxable by the native linker.
                    forbidden.update(range(max(0,x-3),min(sz,x+8)))
        runs=[];a=0
        for k in range(sz+1):
            if k in forbidden or k==sz:
                if k-a>=3:runs.append((a,k))
                a=k+1
        if not runs:continue
        l,r=max(runs,key=lambda x:x[1]-x[0])
        if r-l<9:continue
        key=s[l:min(l+80,r)];where=b.find(key,first,limit);hits=[]
        while where>=0:
            start=where-l
            if all(b[start+x:start+y]==s[x:y] for x,y in runs):hits.append(start)
            if len(hits)>4:break
            where=b.find(key,where+1,limit)
        if len(set(hits))==1:
            yield {'symbol':fn.name,'off':hits[0],'size':sz,'checkedBytes':sum(y-x for x,y in runs)}
for archive in sorted(p.glob('qualified-*libc.a')):
    h=hashlib.sha256(archive.read_bytes()).hexdigest()
    if any(x['sha256']==h for x in report['archives']):continue
    members=set(run('ar','t',archive).decode().splitlines());found=[]
    for n in ('open_nocancel.o','open64_nocancel.o','getsysstats.o','sysconf.o','malloc.o','arena.o','pthread_create.o','sysconf-pthread_stack_min.o'):
        if n not in members:continue
        for row in fingerprint(run('ar','p',archive,n)):
            found.append({**row,'member':n})
    archiveRow={'basename':archive.name,'sha256':h,'availableMembers':sorted(set(x['member'] for x in found)),'matchedSymbols':len(found)}
    for purpose,v in capture.items():
        matches=[x for x in found if x['off']<=v<x['off']+x['size']]
        if matches:report['captured'][purpose]=[{'object':x['member'],'symbol':x['symbol'],'offsetWithinSymbol':v-x['off'],'checkedInvariantBytes':x['checkedBytes'],'symbolSize':x['size']} for x in matches]
    targets={x['off']:x['symbol'] for x in found if x['symbol'] in ('__get_nprocs','get_nprocs','get_nproc_stat','read_sysfs_file','__open64_nocancel','__open_nocancel')}
    calls=[]
    i=b.find(b'\xe8',first,limit)
    while i>=0 and i+5<limit:
        dst=i+5+struct.unpack_from('<i',b,i+1)[0]
        if dst in targets:
            caller=[x for x in found if x['off']<=i<x['off']+x['size']]
            # A real direct call in a matched symbol, or the known return-site control.
            if caller or i+4==capture['possibleReturn']:
                calls.append({'site':hex(i),'target':targets[dst],'namedCallers':sorted(set(x['symbol'] for x in caller)),'matchesObservedReturn':i+4==capture['possibleReturn']})
        i=b.find(b'\xe8',i+1,limit)
    archiveRow['staticCalls']=calls
    report['archives'].append(archiveRow);dump()
assert report['archives']
