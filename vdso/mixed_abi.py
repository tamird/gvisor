"""Exercise this gVisor vDSO's native clock fallback and versioned ABI."""
import ctypes
import hashlib
import json
import mmap
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys
import time

def sha(b): return hashlib.sha256(b).hexdigest()

image,checker,reader=map(Path,sys.argv[1:4])
arch=platform.machine()
machine={"x86_64":62,"aarch64":183}[arch]
version={"x86_64":"LINUX_2.6","aarch64":"LINUX_2.6.39"}[arch]
data=image.read_bytes()
assert data[:6]==b"\x7fELF\x02\x01" and struct.unpack_from("<HH",data,16)==(3,machine)
subprocess.run([sys.executable,str(checker),"--check-data","--vdso="+str(image),"--readelf="+str(reader)],check=True)
table=subprocess.check_output([str(reader),"--dyn-syms","-W",str(image)],text=True)
functions={m[2]:(int(m[1],16),m[3]) for m in re.finditer(r"^\s*\d+:\s+([0-9a-fA-F]+)\s+\d+\s+FUNC\s+(?:GLOBAL|WEAK)\s+DEFAULT\s+\d+\s+(\w+)@@(LINUX_[.\d]+)\s*$",table,re.M)}
expected={"__kernel_rt_sigreturn"}
if arch=="x86_64":
    for name in ("getcpu","clock_gettime","time","gettimeofday"):
        expected.update((name,"__vdso_"+name))
        assert functions[name]==functions["__vdso_"+name]
    clk="__vdso_clock_gettime"
else:
    expected.update(("__kernel_clock_getres","__kernel_clock_gettime","__kernel_gettimeofday"))
    clk="__kernel_clock_gettime"
assert set(functions)==expected and all(v[1]==version for v in functions.values())
offset=struct.unpack_from("<Q",data,32)[0]
entsize,count=struct.unpack_from("<HH",data,54)
ph=[struct.unpack_from("<IIQQQQQQ",data,offset+i*entsize) for i in range(count)]
loads=[p for p in ph if p[0]==1]; assert loads and not any(p[0]==3 for p in ph)
addr=functions[clk][0]
seg,=[p for p in loads if p[1]&1 and p[3]<=addr<p[3]+p[5]]
clock_offset=seg[2]+addr-seg[3]
class Timespec(ctypes.Structure):
    _fields_=[("sec",ctypes.c_long),("nsec",ctypes.c_long)]
with mmap.mmap(-1,len(data),flags=mmap.MAP_PRIVATE|mmap.MAP_ANONYMOUS,prot=mmap.PROT_READ|mmap.PROT_WRITE) as mapped:
    mapped.write(data)
    ptr=ctypes.addressof(ctypes.c_char.from_buffer(mapped))
    libc=ctypes.CDLL(None,use_errno=True)
    assert libc.mprotect(ctypes.c_void_p(ptr),ctypes.c_size_t(len(data)),mmap.PROT_READ|mmap.PROT_EXEC)==0
    read_clock=ctypes.CFUNCTYPE(ctypes.c_int,ctypes.c_int,ctypes.POINTER(Timespec))(ptr+clock_offset)
    ts=Timespec()
    before=time.clock_gettime_ns(time.CLOCK_PROCESS_CPUTIME_ID)
    assert read_clock(time.CLOCK_PROCESS_CPUTIME_ID,ctypes.byref(ts))==0
    after=time.clock_gettime_ns(time.CLOCK_PROCESS_CPUTIME_ID)
    assert 0<=ts.nsec<1_000_000_000 and before<=ts.sec*1_000_000_000+ts.nsec<=after
print("GVISOR_VDSO "+json.dumps({"arch":arch,"machine":machine,"sha256":sha(data),"size":len(data),
    "load_sha256":sha(b"".join(data[p[2]:p[2]+p[5]] for p in loads)),"version":version,"symbols":sorted(functions),
    "symbol_offsets":{k:v[0]-loads[0][3] for k,v in functions.items()},"checked":True,"called":clk},sort_keys=True))
