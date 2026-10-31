"""Capture only the original Sentry's fatal openat at Linux force_sig_seccomp."""
import ctypes as c
import json
import os
from pathlib import Path
import re
import subprocess
import time
from bcc import BPF

temp=Path(os.environ['RUNNER_TEMP']);out=temp/'receipt'
binary=temp/'reused/gvisor-bin/gvisor_sentry_plugin_stack';ino=binary.stat()
sink=out/'denied-openat.json';report={'processes':{},'fatal':[],'priorELFOffset':'0xe89b3a'}
def save():sink.write_text(json.dumps(report,indent=2)+'\n')
def cmd(*args):return subprocess.check_output(args,text=True,stderr=subprocess.DEVNULL)
loads=[]
for s in cmd('readelf','-lW',binary).splitlines():
    x=s.split()
    if x and x[0]=='LOAD':loads.append(tuple(int(x[i],16) for i in (1,2,4)))
with binary.open('rb') as f:f.seek(0xe89b3a);assert f.read(2)==b'\x0f\x05'
save()
src=r'''
#include <linux/sched.h>
#include <linux/ptrace.h>
#include <asm/thread_info.h>
#include <asm/processor.h>
struct event { u32 pid, tid; s32 dirfd, stackid, pathret, original; u64 flags, rip, stack[24]; char path[192]; };
BPF_PERCPU_ARRAY(tmp,struct event,1);
BPF_HASH(active, u32, u32, 64);
BPF_STACK_TRACE(stacks,64);
BPF_PERF_OUTPUT(events);
int fatal_open(struct pt_regs *ctx) {
    if ((int)PT_REGS_PARM1(ctx)!=257) return 0;
    u64 ident=bpf_get_current_pid_tgid();u32 pid=ident>>32;
    if (!active.lookup(&pid)) return 0;
    struct task_struct *task=(void *)bpf_get_current_task();
    unsigned long kstack=0;
    bpf_probe_read_kernel(&kstack,sizeof(kstack),&task->stack);
    if (!kstack) return 0;
    struct pt_regs *user=(struct pt_regs *)(kstack + THREAD_SIZE - TOP_OF_KERNEL_STACK_PADDING)-1;
    u32 zero=0;struct event *pe=tmp.lookup(&zero);if (!pe) return 0;
    pe->pid=pid;pe->tid=(u32)ident;
    u64 arg=0,sp=0; long nr=0;
    bpf_probe_read_kernel(&nr,sizeof(nr),&user->orig_ax);pe->original=nr;
    bpf_probe_read_kernel(&pe->rip,sizeof(pe->rip),&user->ip);
    bpf_probe_read_kernel(&arg,sizeof(arg),&user->di);pe->dirfd=(int)arg;
    bpf_probe_read_kernel(&pe->flags,sizeof(pe->flags),&user->dx);
    bpf_probe_read_kernel(&arg,sizeof(arg),&user->si);
    pe->pathret=bpf_probe_read_user_str(pe->path,sizeof(pe->path),(void *)arg);
    bpf_probe_read_kernel(&sp,sizeof(sp),&user->sp);
    bpf_probe_read_user(pe->stack,sizeof(pe->stack),(void *)sp);
    pe->stackid=stacks.get_stackid(ctx,BPF_F_USER_STACK);
    events.perf_submit(ctx,pe,sizeof(*pe));return 0;
}
'''
b=BPF(text=src);b.attach_kprobe(event='force_sig_seccomp',fn_name='fatal_open')
maps={};seen=set();raw=[]
def event(cpu,p,size):
    x=b['events'].event(p)
    raw.append((int(x.pid),int(x.tid),int(x.original),int(x.dirfd),int(x.flags),int(x.rip),int(x.pathret),bytes(x.path).decode('ascii','replace'),int(x.stackid),list(x.stack)))
b['events'].open_perf_buffer(event)
report['probeAttached']=True;save()
(temp/'path.ready').touch();earliest=int(time.time())
while not (temp/'path.stop').exists():
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:
            st=(p/'exe').stat()
            if (st.st_dev,st.st_ino)!=(ino.st_dev,ino.st_ino):continue
            pid=int(p.name)
            if pid not in seen:
                b['active'][c.c_uint(pid)]=c.c_uint(1);seen.add(pid)
                report['processes'][p.name]={'affinity':sorted(os.sched_getaffinity(pid))}
            regions=[]
            for line in (p/'maps').read_text().splitlines():
                a=line.split(maxsplit=5)
                if len(a)<6 or int(a[4])!=ino.st_ino or 'x' not in a[1]:continue
                lo,hi=[int(v,16) for v in a[0].split('-')];regions.append((lo,hi,int(a[2],16)))
            if regions:maps[pid]=regions
        except OSError:continue
    b.perf_buffer_poll(timeout=12)
b.perf_buffer_poll(timeout=50)
def frame(pid,ip):
    off=next((o+ip-lo for lo,hi,o in maps.get(pid,[]) if lo<=ip<hi),None)
    if off is None:return None
    v=next((a+off-o for o,a,n in loads if o<=off<o+n),None)
    if v is None:return None
    y=cmd('addr2line','-f','-C','-e',binary,hex(v)).splitlines()
    f={'elfOffset':hex(off),'symbol':y[0],'source':y[1].split('/')[-1] if len(y)>1 else None}
    if f['symbol']=='??':
        try:
            g=subprocess.run(['go','tool','addr2line',binary],input=format(v,'x')+'\n',text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=4)
            ans=g.stdout.splitlines()
            if len(ans)>1 and ans[0]!='?':f['goSymbol']=ans[0];f['goSource']=ans[1].split('/')[-1]
        except (OSError,subprocess.TimeoutExpired):pass
    return f
for pid,tid,nr,dfd,flags,rip,pathret,path,stackid,slots in raw:
    pth = path if len(path)<193 and re.fullmatch(r'[A-Za-z0-9_./:+@-]+',path) and (path.startswith(('/usr/','/sys/','/proc/','/dev/','/etc/','/run/','/lib','/tmp/')) or '/' not in path) else '<redacted>'
    row={'pid':pid,'tid':tid,'registerSyscall':nr,'dirfd':dfd,'flags':hex(flags),'pathReadResult':pathret,'pathname':re.sub(r'[a-f0-9]{24,}','<id>',pth),'syscallSite':frame(pid,rip-2),'stackID':stackid,'userFrames':[],'possibleReturnSites':[]}
    if stackid>=0:
        for i,ip in enumerate(b['stacks'].walk(stackid)):
            site=frame(pid,ip-(i!=0))
            if site:row['userFrames'].append(site)
    for i,ip in enumerate(slots):
        site=frame(pid,ip-1)
        if site:row['possibleReturnSites'].append({'slot':i,**site})
    report['fatal'].append(row);save()
logs=cmd('journalctl','-k','--since','@'+str(earliest),'--no-pager','-o','cat')
report['audit']=[]
for line in logs.splitlines():
    if not re.search(r'\btype=1326\b|\bseccomp\b',line,re.I):continue
    f=dict(re.findall(r'\b(pid|sig|syscall|code|arch)=((?:0x)?[0-9a-f]+)',line))
    if f.get('pid','').isdigit() and int(f['pid']) in seen and f.get('syscall')=='257' and f.get('sig')=='31':report['audit'].append(f)
save()
