"""Read ELF64 and the Go function table, including stripped binaries."""
import hashlib
import struct


def require(condition, rule):
    if not condition:
        raise ValueError(rule)


def elf(p):
    raw=p.read_bytes()
    require(raw[:6]==b"\x7fELF\x02\x01","ELF64 little endian")
    et, machine=struct.unpack_from("<HH",raw,16)
    phoff,shoff=struct.unpack_from("<QQ",raw,32)
    phsize,phcount,shsize,shcount,names=struct.unpack_from("<5H",raw,54)
    ptypes=[struct.unpack_from("<I",raw,phoff+i*phsize)[0] for i in range(phcount)]
    sh=[struct.unpack_from("<IIQQQQIIQQ",raw,shoff+i*shsize) for i in range(shcount)]
    strings=raw[sh[names][4]:sh[names][4]+sh[names][5]]
    needed=False
    live=set()
    for s in sh:
        name=strings[s[0]:].split(b"\0",1)[0]
        b=raw[s[4]:s[4]+s[5]]
        if s[1]==6:  # SHT_DYNAMIC; static PIE itself may have PT_DYNAMIC.
            needed |= any(struct.unpack_from("<q",b,n)[0]==1 for n in range(0,len(b),16))
        if name not in (b".gopclntab",b".data.rel.ro.gopclntab",b".rodata",b".data.rel.ro"):continue
        marker=b"\xf1\xff\xff\xff\0\0" + bytes([{62: 1, 183: 4}[machine], 8])
        pos=b.find(marker)
        while pos>=0:
            if pos+72<=len(b):
                n,_,_,fname,_,_,_,ftab=struct.unpack_from("<8Q",b,pos+8)
                if 0<n<2000000 and 0<fname<ftab and pos+ftab+n*8<=len(b):
                    for i in range(n):
                        off=struct.unpack_from("<I",b,pos+ftab+i*8+4)[0]
                        entry=pos+ftab+off
                        if entry+8>len(b):continue
                        idx=struct.unpack_from("<i",b,entry+4)[0]
                        if idx<0:continue
                        start=pos+fname+idx
                        for sym in (b"runtime.raceinit",b"runtime.racefini"):
                            if b[start:start+len(sym)+1]==sym+b"\0":live.add(sym.decode())
            pos=b.find(marker,pos+1)
    return {"machine":machine,"elf_type":et,"static":3 not in ptypes and not needed,"race_functions":sorted(live),
            "bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}
