"""Inspect both native-toolchain systrap images and their mapped vDSOs."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys

SOURCE = "1f9c6adcce6f1b04d8b2cb6434d2268d21568588"
BASE = "f6df263f8e2ce2cbe6a497356613f6785c4800a4"
PKG = Path("pkg/sentry/platform/systrap/sysmsg")
TAG = "//" + str(PKG) + ":"
WORK = Path(os.environ["RUNNER_TEMP"]) / "linked-stub"
REPORT = {"source": SOURCE, "base": BASE, "architectures": {}, "verified": False}


def save():
    (WORK / "receipt.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def rule(src, name, template):
    return f'\narch_genrule(name = "{name}", src = ":{src}", template = "{template}")\n'


def prepare():
    f = PKG / "BUILD"
    current = f.read_text()
    marker = "freestanding_cc_binary(\n"
    def producer(txt):
        start = txt.index(marker)
        return txt[start:txt.index("\n)\n", start) + 3]
    cc = producer(current)
    before = subprocess.check_output(["git", "show", BASE + ":" + str(f)], text=True)
    assert cc == producer(before) and "$(OBJCOPY)" in before and "$(OBJCOPY)" not in current
    extractor = subprocess.check_output(["git", "hash-object", PKG / "gen_stub/main.go"], text=True).strip()
    assert extractor == "40288242011693194a326ae023588eeab5251981"
    REPORT["approved_extractor"] = extractor
    REPORT["native_cc_rule"] = sha256(cc.encode()).hexdigest()
    original = (PKG / "pie.lds.S").read_text()
    fields = ("dynsym", "dynstr", "dynamic", "rela.dyn")
    audit = original
    for key in fields:
        token = f"*(.{key})"
        assert audit.count(token) == 1
        audit = audit.replace(token, "")
    audit = audit.replace("        /DISCARD/", "".join(f"        .{k} : {{ *(.{k}) }}\n" for k in (*fields, "rel.dyn", "relr.dyn")) + "        /DISCARD/")
    # The real linker script discards dynamic relocations. Retain them in a
    # diagnostic relink, change the image address, and use an absolute pointer
    # to check that the comparison can see an actual load-time dependency.
    (PKG / "qualify_pointer.c").write_text("void *qualify_absolute_pointer = &qualify_absolute_pointer;\n")
    additions = rule("sighandler_object", "qualify_stub", "qualify.stub.%s.elf")
    targets = [TAG+"sighandler_binary_arch", TAG+"sighandler_go_arch", TAG+"qualify_stub"]
    for name, address, ptr in (("retained", "0x0", False), ("rebased", "0x200000", False),
                               ("pointer_zero", "0x0", True),
                               ("pointer_high", "0x200000", True)):
        script = "qualify_" + name + ".lds"
        assert audit.count(".crblob 0x0") == 1
        (PKG / script).write_text(audit.replace(".crblob 0x0", ".crblob " + address))
        body = cc.replace('name = "sighandler_object"', f'name = "qualify_{name}_link"').replace(
            'out = "sighandler.built-in.bin.o"', f'out = "qualify.{name}.o"').replace("pie.lds.S", script)
        if ptr:
            body = body.replace('        "sysmsg_lib.c",', '        "sysmsg_lib.c",\n        "qualify_pointer.c",')
        additions += "\n" + body + rule("qualify_" + name + "_link", "qualify_" + name, f"qualify.{name}.%s.elf")
        targets.append(TAG+"qualify_"+name)
    f.write_text(current + additions)
    vd = Path("vdso/BUILD")
    text = vd.read_text().replace('load("//tools:arch.bzl", "select_arch")', 'load("//tools:arch.bzl", "arch_genrule", "select_arch")')
    vd.write_text(text + rule("vdso.so", "qualify_vdso", "qualify.vdso.%s.so"))
    targets.append("//vdso:qualify_vdso")
    return targets


class ELF:
    def __init__(self, path):
        self.path = path
        self.data = path.read_bytes()
        et, self.machine, _, _, ph, sh, _, _, psz, nph, ssz, nsh, ni = struct.unpack_from("<HHIQQQIHHHHHH", self.data, 16)
        self.type = et
        assert self.data[:6] == b"\x7fELF\x02\x01" and psz == 56 and ssz == 64
        self.ph = [struct.unpack_from("<IIQQQQQQ", self.data, ph+i*psz) for i in range(nph)]
        ss = [struct.unpack_from("<IIQQQQIIQQ", self.data, sh+i*ssz) for i in range(nsh)]
        self.sections = {self.word(ss[ni], s[0]): s for s in ss}
        self.symbols = {}
        sy = self.sections[".symtab"]
        for i in range(0, sy[5], sy[9]):
            name, _, _, index, value, _ = struct.unpack_from("<IBBHQQ", self.data, sy[4]+i)
            if name:
                key = self.word(ss[sy[6]], name)
                self.symbols[key] = (index,value)

    def word(self, table, position):
        pos = table[4] + position
        return self.data[pos:self.data.index(0,pos,table[4]+table[5])].decode()

    def stub(self):
        s = self.sections[".crblob"]
        assert s[2] & 2 and s[1] == 1
        return s[3], self.data[s[4]:s[4]+s[5]]

    def info(self):
        dyn = subprocess.check_output(["readelf","-dW",self.path], text=True)
        reloc = {name: s[5] // s[9] for name,s in self.sections.items()
                 if s[1] in (4,9,19) and s[2] & 2 and s[5]}
        return {"machine":self.machine,"elf_type":self.type,
                "interpreter":any(p[0]==3 for p in self.ph),
                "needed":re.findall(r"\(NEEDED\).*?\[([^]]+)\]",dyn),
                "undefined":sorted(n for n,(i,_) in self.symbols.items() if i==0),
                "dynamic_relocations":reloc,
                "loads":[{"offset":p[2],"address":p[3],"flags":p[1],"file_size":p[5],"memory_size":p[6],"align":p[7]} for p in self.ph if p[0]==1],
                "sha256":sha256(self.data).hexdigest()}


def inspect(arch, machine, rec):
    root=Path("bazel-bin")/PKG
    raw=(root/f"sighandler.built-in.{arch}.bin").read_bytes()
    go=(root/f"sighandler_{arch}.go").read_text()
    normal=ELF(root/f"qualify.stub.{arch}.elf")
    def record_stub(key, elf):
        rec[key] = elf.info()
        base, blob = elf.stub()
        rec[key].update(blob_address=base,blob_size=len(blob),blob_sha256=sha256(blob).hexdigest())
        save()

    record_stub("stub", normal)
    offsets={n:v for n,(index,v) in normal.symbols.items() if index and n.startswith("__export_")}
    generated={n:int(v,16) for n,v in re.findall(r"^var Sighandler_blob_offset__(\S+) = 0x([0-9a-f]+)$",go,re.M)}
    rec["offsets"]=offsets
    rec["embedded"]=sha256(raw).hexdigest()
    rec["offsets_match"]=offsets==generated and all(0<=v<len(raw) for v in generated.values())
    rec["raw_matches"]=normal.stub()==(0,raw)
    rec["go_sha256"]=sha256(go.encode()).hexdigest()
    save()
    images = {}
    for name in ("retained", "rebased", "pointer_zero", "pointer_high"):
        path = root / f"qualify.{name}.{arch}.elf"
        if path.exists():
            images[name] = ELF(path)
            record_stub(name, images[name])
    assert set(images) == {"retained", "rebased", "pointer_zero", "pointer_high"}
    rec["retained_matches"]=images["retained"].stub()==(0,raw)
    rec["rebased_matches"]=images["rebased"].stub()==(0x200000,raw)
    rec["absolute_pointer_control"]=bool(rec["pointer_high"]["dynamic_relocations"] or images["pointer_zero"].stub()[1]!=images["pointer_high"].stub()[1])
    rec["stub_verified"]=bool(normal.machine==machine and all(e.machine==machine for e in images.values())
        and rec["raw_matches"] and rec["retained_matches"] and rec["rebased_matches"] and rec["absolute_pointer_control"] and rec["offsets_match"]
        and len(offsets)=={"amd64":10,"arm64":11}[arch]
        and not any(rec[k][f] for k in ("stub","retained","rebased") for f in ("interpreter","needed","undefined","dynamic_relocations")))
    save()


def inspect_vdso(arch, machine, rec):
    v=ELF(Path("bazel-bin/vdso")/f"qualify.vdso.{arch}.so")
    vi=rec["vdso"]=v.info()
    checker=subprocess.run([sys.executable,"vdso/check_vdso.py","--check-data","--vdso="+str(v.path)],capture_output=True)
    vi["mapping_check"]=checker.returncode
    ds=subprocess.check_output(["readelf","--dyn-syms","-W",v.path],text=True)
    funcs={m[2]:{"address":int(m[1],16),"version":m[3]} for m in re.finditer(r"^\s*\d+:\s+([0-9a-f]+)\s+\d+\s+FUNC\s+(?:GLOBAL|WEAK)\s+DEFAULT\s+\d+\s+(\w+)@@(LINUX_[.\d]+)\s*$",ds,re.M)}
    vi["exports"]=funcs
    expected={"__kernel_rt_sigreturn"}
    if arch=="amd64":
        for n in ("clock_gettime","gettimeofday","time","getcpu"):
            expected.update((n,"__vdso_"+n))
        version="LINUX_2.6"
    else:
        expected.update(("__kernel_clock_getres","__kernel_clock_gettime","__kernel_gettimeofday"))
        version="LINUX_2.6.39"
    vi["abi_matches"]=expected==funcs.keys() and all(f["version"]==version for f in funcs.values())
    save()
    vi["verified"]=bool(v.machine==machine and vi["abi_matches"] and vi["mapping_check"]==0 and v.type==3 and vi["loads"]
        and not any(vi[k] for k in ("interpreter","needed","undefined","dynamic_relocations")))
    save()


def main():
    targets=prepare()
    save()
    cmd=[WORK/"bazel","--nosystem_rc","--nohome_rc","build","-c","opt","--strip=never",
         "--jobs=4","--keep_going","--color=no","--noshow_progress","--build_event_json_file="+str(WORK/"events.json"),*targets]
    REPORT["exit"]=subprocess.run(cmd).returncode
    save()
    for arch,machine in (("amd64",62),("arm64",183)):
        r=REPORT["architectures"][arch]={}
        for fn in (inspect_vdso, inspect):
            try:
                fn(arch,machine,r)
            except (AssertionError,KeyError,IndexError,ValueError,OSError,subprocess.SubprocessError) as e:
                r[fn.__name__+"_diagnostic"]=type(e).__name__+": "+str(e)[:180]
                save()
        r["verified"]=r.get("stub_verified") is True and r.get("vdso",{}).get("verified") is True
        save()
    REPORT["verified"]=REPORT["exit"]==0 and all(r.get("verified") for r in REPORT["architectures"].values())
    save()
    return 0 if REPORT["verified"] else 1


if __name__=="__main__":
    sys.exit(main())
