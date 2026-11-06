"""Fork-only controls. Change ELF headers in copies of the actual linked vDSOs."""
from hashlib import sha256
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

checker=Path(sys.argv[1]).resolve()
assert checker.read_bytes()[:4]==b"\x7fELF"
root=Path("vdso").resolve();tmp=Path(os.environ["TEST_TMPDIR"])
report={"arches":{},"fail":[]}
clean=dict(os.environ,PATH="/no-tools-in-this-test")
debian=dict(clean,PATH="/usr/bin:/bin",GO111MODULE="off",GOTOOLCHAIN="local",
            CGO_ENABLED="0",GOOS="",GOARCH="",GOCACHE=str(tmp/"go-cache"),GOPROXY="off")
def exercise(arch,name,blob,needle=None,data=True):
    path=tmp/f"{arch}-{name}.so";path.write_bytes(blob)
    args=["--vdso="+str(path)]+(["--check-data"] if data else [])
    current=subprocess.run([checker,*args],env=clean,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    old=subprocess.run([sys.executable,root/"legacy_checker.py",*args],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    item={"go":current.returncode,"python":old.returncode,"expected":needle,"elf_sha256":sha256(blob).hexdigest()}
    is_ok=current.returncode==0 if needle is None else current.returncode!=0 and needle in current.stdout
    if not is_ok:
        report["fail"].append([arch,name,current.stdout[:220]])
    return item

for arch,machine in (("amd64",62),("arm64",183)):
    try:
        good=(root/f"qualify.good.{arch}.so").read_bytes()
        original=(root/f"qualify.baseline.{arch}.so").read_bytes()
        bad=(root/f"qualify.data.{arch}.so").read_bytes()
        rows=report["arches"][arch]={"sha256":sha256(good).hexdigest(),"before_sha256":sha256(original).hexdigest(),
                                      "data_sha256":sha256(bad).hexdigest(),"bytes":len(good),"machine":struct.unpack_from("<H",good,18)[0]}
        assert good[:6]==b"\x7fELF\x02\x01" and struct.unpack_from("<H",good,18)[0]==machine
        assert original==good,(arch,"changed vDSO ELF")
        rows["valid"]=exercise(arch,"valid",good)
        rows["linked_data"]=exercise(arch,"data",bad,"unexpected nonempty section .data")
        rows["optional_data"]=exercise(arch,"optional",bad,data=False)
        # The real Debian recipe runs its host compiler against a target CPU's
        # binary. Use the packaged Debian/Ubuntu Go tools and no module downloads.
        for case,blob,want in [("valid",good,True),("data",bad,False)]:
            artifact=tmp/("debian-"+arch+"-"+case+".so");artifact.write_bytes(blob)
            cmd=["/usr/bin/go","run","./vdso/check","--check-data","--vdso="+str(artifact)]
            p=subprocess.run(cmd,env=debian,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
            assert (p.returncode==0)==want and (want or "unexpected nonempty section .data" in p.stdout),(arch,case,p.stdout[:200])
            rows["debian_"+case]={"exit":p.returncode}
        hdr=struct.unpack_from("<16sHHIQQQIHHHHHH",good)
        phoff,shoff,phsz,phnum,shsz,shnum,shnames=hdr[5],hdr[6],hdr[9],hdr[10],hdr[11],hdr[12],hdr[13]
        ph=[phoff+i*phsz for i in range(phnum)];sh=[shoff+i*shsz for i in range(shnum)]
        pt=lambda raw,pos:struct.unpack_from("<IIQQQQQQ",raw,pos)
        loads=[p for p in ph if pt(good,p)[0]==1];assert len(loads)==1
        first=loads[0]; extra=next(p for p in ph if p>first and pt(good,p)[0]!=1)
        base=pt(good,first)[3];assert base%4096==0 and len(good)>4097
        sec=lambda raw,pos:struct.unpack_from("<IIQQQQIIQQ",raw,pos)
        stroff=sec(good,sh[shnames])[4]
        names={good[stroff+sec(good,p)[0]:].split(b"\0",1)[0].decode():p for p in sh}
        def u32(pos,val,blob=None):
            b=bytearray(good if blob is None else blob);struct.pack_into("<I",b,pos,val);return b
        def u64(pos,val,blob=None):
            b=bytearray(good if blob is None else blob);struct.pack_into("<Q",b,pos,val);return b
        rows["no_load"]=exercise(arch,"no-load",u32(first,0),"no PT_LOAD")
        rows["offset"]=exercise(arch,"offset",u64(first+8,1),"has file offset")
        rows["zero_fill"]=exercise(arch,"zerofill",u64(first+40,pt(good,first)[5]+1),"memory size")
        def pair(first_size,next_offset):
            b=u64(first+32,first_size)
            struct.pack_into("<Q",b,first+40,first_size)
            struct.pack_into("<IIQQQQQQ",b,extra,1,5,next_offset,base+next_offset,base+next_offset,1,1,4096)
            return b
        rows["overlap"]=exercise(arch,"overlap",pair(1800,1200),"overlaps or precedes")
        rows["same_page"]=exercise(arch,"samepage",pair(1024,2048),"same page")
        rows["boundary"]=exercise(arch,"boundary",pair(4096,4096),"same page")
        rows["next_page"]=exercise(arch,"nextpage",pair(4095,4096))
        rows["empty_text"]=exercise(arch,"emptytext",u64(names[".text"]+32,0),"no nonempty .text")
        # Rewrite the name of a real nonempty section to a .bss or .data suffix.
        ro=names[".rodata"] if ".rodata" in names else names[".dynstr"]
        for nm in (b".bss.x",b".data.x"):
            off=stroff+sec(good,ro)[0];b=bytearray(good);b[off:off+len(nm)+1]=nm+b"\0"
            rows[nm.decode()]=exercise(arch,nm.decode()[1:],b,"unexpected nonempty section")
        for typ in (4,9,19):
            rows["section_relocation_"+str(typ)]=exercise(arch,"sectionrel"+str(typ),u32(names[".text"]+4,typ),"relocation section")
        dynamic=next(p for p in sh if sec(good,p)[1]==6)
        start=sec(good,dynamic)[4];length=sec(good,dynamic)[5]
        harmless=next(p for p in range(start,start+length,16) if struct.unpack_from("<Q",good,p)[0]==14)
        for tag in (2,8,18,35):
            b=u64(harmless,tag);b=u64(harmless+8,8,b)
            rows["dynamic_relocation_"+str(tag)]=exercise(arch,"dynrel"+str(tag),b,"dynamic relocation tag")
        # Readelf may ignore size-only dynamic tags. Preserve what each actually did.
    except (OSError, AssertionError, ValueError, KeyError, StopIteration, struct.error) as exc:
        report["fail"].append([arch,type(exc).__name__,str(exc)[:400]])
print("CHECKER-RECEIPT "+json.dumps(report,sort_keys=True))
assert not report["fail"]
