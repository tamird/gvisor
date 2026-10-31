"""Read the fatal syscall's registers from the original x86-64 Sentry at exit."""
import ctypes as ct
from ctypes.util import find_library
import errno
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

temp = Path(os.environ['RUNNER_TEMP'])
binary = temp/'reused/gvisor-bin/gvisor_sentry_plugin_stack'
ino = binary.stat()
stop = temp/'process-observer.stop'
dest = temp/'receipt/process-observer.json'
receipt = {'hostAffinity': sorted(os.sched_getaffinity(0)), 'processes':{}, 'selected':[], 'errors':{}, 'audit':[]}
def dump(): dest.write_text(json.dumps(receipt,indent=2)+'\n')

lib=ct.CDLL(None,use_errno=True)
lib.ptrace.restype=ct.c_long
def call(op,tid,address=0,data=0):
    ct.set_errno(0)
    r=lib.ptrace(ct.c_uint(op),ct.c_int(tid),ct.c_void_p(address),ct.c_void_p(data))
    if r==-1 and ct.get_errno():raise OSError(ct.get_errno(),os.strerror(ct.get_errno()))
    return r
def ptr(tid,addr):
    return call(2,tid,addr)&((1<<64)-1)
def getstr(tid,address):
    b=bytearray()
    for n in range(0,192,8):
        b.extend(ptr(tid,address+n).to_bytes(8,'little'))
        if 0 in b: break
    return b.split(b'\0')[0].decode('ascii','replace')
names='r15 r14 r13 r12 rbp rbx r11 r10 r9 r8 rax rcx rdx rsi rdi orig_rax rip cs eflags rsp ss fs_base gs_base ds es fs gs'.split()
class Regs(ct.Structure):
    _fields_=[(k,ct.c_ulonglong) for k in names]
def errors(stage,exc):
    key=stage+':'+str(exc.errno if isinstance(exc,OSError) else type(exc).__name__)
    receipt['errors'][key]=receipt['errors'].get(key,0)+1

# UPT reads the same already-stopped tracee via ptrace. No cores, unfiltered
# stacks, environment strings, or memory pages are written to the receipt.
def fn(dll,name,ret,*args):
    f=getattr(dll,name); f.restype=ret;f.argtypes=args;return f
def unwind(tid):
    word=ct.c_ulonglong
    ux=ct.CDLL(find_library('unwind-x86_64'),mode=ct.RTLD_GLOBAL)
    dl=ct.CDLL(find_library('unwind-ptrace'),mode=ct.RTLD_GLOBAL)
    acc=ct.c_byte.in_dll(dl,'_UPT_accessors')
    aspace=fn(ux,'_Ux86_64_create_addr_space',ct.c_void_p,ct.c_void_p,ct.c_int)(ct.addressof(acc),0)
    cookie=fn(dl,'_UPT_create',ct.c_void_p,ct.c_int)(tid)
    cur=ct.create_string_buffer(4096)
    try:
        init=fn(ux,'_Ux86_64_init_remote',ct.c_int,ct.c_void_p,ct.c_void_p,ct.c_void_p)(cur,aspace,cookie)
        if init<0:return [],{'init':init}
        reg=fn(ux,'_Ux86_64_get_reg',ct.c_int,ct.c_void_p,ct.c_int,ct.POINTER(word))
        name=fn(ux,'_Ux86_64_get_proc_name',ct.c_int,ct.c_void_p,ct.c_void_p,ct.c_size_t,ct.POINTER(word))
        step=fn(ux,'_Ux86_64_step',ct.c_int,ct.c_void_p)
        frames=[]
        for i in range(32):
            ip=word(); n=ct.create_string_buffer(512);off=word()
            if reg(cur,16,ct.byref(ip))<0:break
            code=name(cur,n,len(n),ct.byref(off)); s=n.value.decode('ascii','replace')
            frames.append((ip.value,s if code>=0 and re.fullmatch(r'[\w.$<>~:+*/@()\[\], -]{1,450}',s) else None))
            st=step(cur)
            if st<=0:break
        return frames,{'init':init,'frames':len(frames)}
    finally:
        fn(dl,'_UPT_destroy',None,ct.c_void_p)(cookie)
        fn(ux,'_Ux86_64_destroy_addr_space',None,ct.c_void_p)(aspace)

def mappings(tid):
    regions=[]
    for line in Path(f'/proc/{tid}/maps').read_text().splitlines():
        x=line.split(maxsplit=5)
        if len(x)<6 or int(x[4])!=ino.st_ino or 'x' not in x[1]:continue
        start,end=(int(v,16) for v in x[0].split('-'))
        regions.append((start,end,int(x[2],16)))
    return regions
# Convert file offsets to ELF virtual addresses for GNU addr2line/objdump.
loads=[]
for line in subprocess.check_output(['readelf','-lW',binary],text=True).splitlines():
    a=line.split()
    if a and a[0]=='LOAD':loads.append(tuple(int(a[j],16) for j in (1,2,4)))
def frame(ip,regions):
    foff=next((off+ip-start for start,end,off in regions if start<=ip<end),None)
    if foff is None:return None
    addr=next((va+foff-off for off,va,size in loads if off<=foff<off+size),None)
    if addr is None:return None
    entries=subprocess.check_output(['addr2line','-f','-C','-e',binary,hex(addr)],text=True).splitlines()
    return {'elfOffset':hex(foff),'symbol':entries[0],'source':entries[1].split('/')[-1] if len(entries)>1 else None}
def record(tid,leader,phase,status):
    regs=Regs();call(12,tid,0,ct.addressof(regs))
    if regs.orig_rax!=257:return
    if any(x['tid']==tid for x in receipt['selected']):return
    row={'tid':tid,'leader':leader,'phase':phase,'waitStatus':status,'syscall':257,'dirfd':ct.c_int(regs.rdi).value,'flags':hex(regs.rdx)}
    try:
        s=getstr(tid,regs.rsi)
        # One failed call in a disposable guest fixture. Retain only ordinary
        # system-file path syntax, redacting run-unique IDs from tmp paths.
        row['pathname']=re.sub(r'[a-f0-9]{20,}','<id>',s) if ((s.startswith(('/sys/','/proc/','/etc/','/dev/','/usr/','/lib','/tmp/','/run/')) or s in ('localtime','/')) and re.fullmatch(r'[A-Za-z0-9_./:+@-]{1,192}',s)) else '<redacted other pathname>'
    except OSError as e:errors('pathname',e)
    try:
        regions=mappings(tid)
        row['syscallSite']=frame(regs.rip-2,regions)
        frames,meta=unwind(tid)
        row['unwind']=meta;resolved=[]
        for i,(ip,name) in enumerate(frames):
            site=frame(ip-(i!=0),regions)
            if site:
                if name:site['unwindSymbol']=name
                resolved.append(site)
        row['frames']=resolved
    except Exception as e:errors('unwind',e)
    receipt['selected'].append(row);dump()
earliest=int(time.time());deadline=time.monotonic()+330;attached={};seen=set()
(temp/'process-observer.ready').touch();dump()
while time.monotonic()<deadline and (not stop.exists() or (attached and time.monotonic()<deadline-1)):
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:
            st=(p/'exe').stat()
            if (st.st_dev,st.st_ino)!=(ino.st_dev,ino.st_ino):continue
            leader=int(p.name);seen.add(leader)
            r=receipt['processes'].setdefault(p.name,{'allowed': sorted(os.sched_getaffinity(leader)), 'attachedThreads':0})
            for t in (p/'task').iterdir():
                tid=int(t.name)
                if tid in attached:continue
                try:call(0x4206,tid,0,0x40) # SEIZE + TRACEEXIT only; no detach/reparent of Sentry children.
                except OSError as e:
                    if e.errno not in (errno.ESRCH,):errors('seize',e)
                    continue
                attached[tid]=leader;r['attachedThreads']+=1
        except OSError:continue
    while True:
        try:tid,status=os.waitpid(-1,os.WNOHANG|0x40000000)
        except ChildProcessError:break
        if not tid:break
        if os.WIFEXITED(status) or os.WIFSIGNALED(status):
            attached.pop(tid,None);continue
        if not os.WIFSTOPPED(status) or tid not in attached:continue
        sig=os.WSTOPSIG(status);ev=status>>16
        try:
            if ev==6:
                raw=ct.c_ulong();call(0x4201,tid,0,ct.addressof(raw))
                if raw.value&127==signal.SIGSYS:record(tid,attached[tid],'exit-event',raw.value)
            elif ev==0 and sig==signal.SIGSYS:
                record(tid,attached[tid],'signal-delivery',sig)
        except Exception as ex:errors('inspect',ex)
        finally:
            try:call(7,tid,0,0 if ev else sig)
            except OSError:pass
    if stop.exists() and not attached:break
    time.sleep(.008)
logs=subprocess.run(['journalctl','-k','--since','@'+str(earliest),'--no-pager','-o','cat'],capture_output=True,text=True)
for line in logs.stdout.splitlines():
    if not re.search(r'type=1326|seccomp',line,re.I):continue
    fields=dict(re.findall(r'\b(pid|sig|syscall|code|arch)=(0x[0-9a-f]+|[0-9a-f]+)',line))
    if fields.get('pid','').isdigit() and int(fields['pid']) in seen:receipt['audit'].append(fields)
dump()
