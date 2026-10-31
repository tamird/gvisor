"""Map an unchanged seccomp kill to a file probe on the same immutable ELF."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

phase=sys.argv[1]; temp=Path(os.environ['RUNNER_TEMP']); out=temp/'receipt'
stop=temp/(phase+'.stop'); ready=temp/(phase+'.ready')
binary=temp/'reused/gvisor-bin/gvisor_sentry_plugin_stack'; inode=binary.stat()
report={'phase':phase,'processes':{},'audits':[]}; output=out/(phase+'-kernel.json')
def save():output.write_text(json.dumps(report,indent=2)+'\n')
def cmd(*args):return subprocess.check_output(args,text=True,stderr=subprocess.DEVNULL)
loads=[]
for s in cmd('readelf','-lW',binary).splitlines():
    x=s.split()
    if x and x[0]=='LOAD':loads.append(tuple(int(x[j],16) for j in (1,2,4)))
maps={}
def mapping(pid):
    for s in Path(f'/proc/{pid}/maps').read_text().splitlines():
        a=s.split(maxsplit=5)
        if len(a)<6 or int(a[4])!=inode.st_ino or 'x' not in a[1]:continue
        lo,hi=map(lambda n:int(n,16),a[0].split('-'));yield lo,hi,int(a[2],16)
def fileoff(pid,ip):
    return next((off+ip-lo for lo,hi,off in maps.get(pid,[]) if lo<=ip<hi),None)
def va(off):
    return next((v+off-o for o,v,n in loads if o<=off<o+n),None)
def frame(off):
    if off is None:return None
    v=va(off)
    if v is None:return None
    r=cmd('addr2line','-f','-C','-e',binary,hex(v)).splitlines()
    return {'fileOffset':hex(off),'symbol':r[0],'source':r[1].split('/')[-1] if len(r)>1 else None}
def safe_path(p):
    if len(p)<193 and re.fullmatch(r'[A-Za-z0-9_./:+@-]+',p):
        if p.startswith(('/sys/','/etc/','/proc/','/dev/','/usr/','/lib','/tmp/','/run/')) or ('/' not in p and len(p)<48):
            return re.sub(r'[a-f0-9]{20,}','<id>',p)
    return '<redacted pathname>'
trace=Path('/sys/kernel/tracing')
instance=None; events=[]; tracked=set(); parents={}
def write(p,data):
    with p.open('w') as f:f.write(data+'\n')
def declare(file,group,event,expr):
    with (trace/file).open('a') as f:f.write(f'p:{group}/{event} {expr}\n')
    events.append((file,group,event))
def remove_events():
    if not instance:return
    for f,g,e in reversed(events):
        try:write(instance/'events'/g/e/'enable','0')
        except OSError:pass
    try:instance.rmdir()
    except OSError:pass
    for f,g,e in reversed(events):
        try:
            with (trace/f).open('a') as outp:outp.write(f'-:{g}/{e}\n')
        except OSError:pass
def arm():
    global instance
    orig=json.loads((out/'site-kernel.json').read_text())
    assert len(orig['audits'])==1,orig
    off=int(orig['audits'][0]['site']['fileOffset'],16);v=va(off)
    with binary.open('rb') as f:
        f.seek(off-40);data=f.read(50)
    assert data[40:42]==b'\x0f\x05',data[36:44].hex()
    # A move-immediate into EAX before syscall leaves all six syscall arguments
    # unchanged. When available, trap there so syscall itself still executes
    # at its original IP. Otherwise use the known two-byte syscall instruction.
    probeoff=off-5 if data[35:40]==b'\xb8\x01\x01\x00\x00' else off
    report['probeSite']=frame(probeoff);report['atSyscall']=probeoff==off
    instance=trace/'instances/tldkq'
    instance.mkdir()
    write(instance/'set_event_pid',str(os.getpid()))
    write(instance/'buffer_size_kb','4096')
    write(instance/'options/userstacktrace','1')
    if (instance/'options/sym-userobj').exists():write(instance/'options/sym-userobj','0')
    group='tldkq'
    slots=' '.join(f's{i}=$stack{i}:x64' for i in range(12))
    declare('uprobe_events',group,'fatal_site',f'{binary}:{hex(probeoff)} fd=%di:s32 flags=%dx:x64 path=+0(%si):string {slots}')
    write(instance/'events'/group/'fatal_site/enable','1')
    # Known Linux v6.8 prototype: force_sig_seccomp(int syscall,int reason,bool core).
    declare('kprobe_events',group,'kill_open','force_sig_seccomp nr=%di:s32')
    write(instance/'events'/group/'kill_open/filter','nr == 257')
    write(instance/'events'/group/'kill_open/enable','1')
    write(instance/'tracing_on','1')
    save()
def grouped(lines):
    groups=[];current=None
    for line in lines:
        m=re.match(r'^\s*\S.*?-(\d+)\s+.*?\s(\d+\.\d+):\s+(.*)$',line)
        if m:
            current={'tid':int(m[1]),'t':float(m[2]),'event':m[3],'rest':[]}
            groups.append(current)
        elif current is not None:current['rest'].append(line)
    return groups
def decode_trace():
    write(instance/'tracing_on','0')
    rows=grouped((instance/'trace').read_text(errors='replace').splitlines())
    report['traceCounts']={kind:sum(kind in r['event'] for r in rows) for kind in ('fatal_site:','kill_open:','<user stack trace>')}
    denials=[(i,r) for i,r in enumerate(rows) if 'kill_open:' in r['event']]
    resolved=[]
    for i,r in denials:
        tid=r['tid'];pid=parents.get(tid)
        if pid is None or not any(a['pid']==pid for a in report['audits']):continue
        opens=[(j,s) for j,s in enumerate(rows[:i]) if s['tid']==tid and 'fatal_site:' in s['event']]
        if not opens:continue
        j,s=opens[-1];m=re.search(r'\bfd=(-?\d+) flags=(0x[0-9a-f]+|\d+) path="([^"]*)"',s['event'])
        if not m:continue
        row={'tid':tid,'leader':pid,'dirfd':int(m[1]),'flags':m[2],'pathname':safe_path(m[3]),'deltaToKillUs':round((r['t']-s['t'])*1e6)}
        ips=[]
        for e in rows[j:i+2]:
            if e['tid']==tid and ('<user stack trace>' in e['event'] or e is s):
                for line in e['rest']:
                    ips.extend(int(z,16) for z in re.findall(r'(?:=>\s*|<)(?:0x)?([0-9a-fA-F]{5,})(?:>|\s|$)',line))
        row['userFrames']=[x for ip in ips if (x:=frame(fileoff(pid,ip)))]
        slots=[]
        for idx,z in re.findall(r'\bs(\d+)=0x([0-9a-f]+)',s['event']):
            f=frame(fileoff(pid,int(z,16)-1))
            if f:slots.append({'slot':int(idx),**f})
        row['possibleReturnSites']=slots
        resolved.append(row)
    report['selected']=resolved
try:
    if phase=='path':arm()
    earliest=int(time.time());ready.touch();save()
    while not stop.exists():
        tids=set()
        for p in Path('/proc').iterdir():
            if not p.name.isdigit():continue
            try:
                st=(p/'exe').stat()
                if (st.st_dev,st.st_ino)!=(inode.st_dev,inode.st_ino):continue
                pid=int(p.name);maps[pid]=list(mapping(pid));t={int(x.name) for x in (p/'task').iterdir()};tids|=t
                parents.update({n:pid for n in t})
                report['processes'].setdefault(p.name,{'allowed':sorted(os.sched_getaffinity(pid)),'trial':(temp/'arena-trial').read_text() if (temp/'arena-trial').exists() else None})
            except OSError:continue
        if instance and tids-tracked:
            tracked.update(tids)
            write(instance/'set_event_pid',' '.join(str(x) for x in sorted(tracked|{os.getpid()})))
        time.sleep(.009)
    k=cmd('journalctl','-k','--since','@'+str(earliest),'--no-pager','-o','cat')
    for line in k.splitlines():
        if not re.search(r'\btype=1326\b|\bseccomp\b',line,re.I):continue
        f=dict(re.findall(r'\b(pid|sig|syscall|code|arch|ip)=((?:0x)?[0-9a-f]+)',line))
        if not f.get('pid','').isdigit() or f.get('syscall')!='257' or f.get('sig')!='31':continue
        pid=parents.get(int(f['pid']),int(f['pid']))
        if pid not in maps:continue
        rip=int(f.pop('ip','0'),16)
        report['audits'].append({'pid':pid,'sig':31,'syscall':257,'code':f.get('code'),'site':frame(fileoff(pid,rip-2))})
    if instance:decode_trace()
    save()
finally:
    remove_events()
