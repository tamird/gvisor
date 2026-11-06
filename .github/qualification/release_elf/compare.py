"""Compare two hash-verified ELFs offline, publishing only typed, bounded data."""

import collections
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys

from fetch import FILES


ROOT = Path(sys.argv[1])
PUBLIC_SECTIONS = set("""
.text .rodata .gopclntab .data.rel.ro.gopclntab .symtab .strtab .shstrtab .gosymtab
.typelink .itablink .go.buildinfo .go.fipsinfo .go.fipsrodata .go.fipsdata .noptrdata
.data .bss .noptrbss .eh_frame .eh_frame_hdr .note.go.buildid .note.gnu.build-id
.gnu.build.attributes .note.gnu.property .interp .dynstr .dynsym .dynamic .plt .got
.got.plt .data.rel.ro .comment .debug_abbrev .debug_addr .debug_aranges .debug_frame
.debug_info .debug_line .debug_line_str .debug_loc .debug_loclists .debug_ranges
.debug_rnglists .debug_str .debug_str_offsets .debug_gdb_scripts .rela .rela.dyn .rela.plt
""".split())
CONFIG = re.compile(rb"(?:^|/)bazel-out/((?:aarch64|arm64|k8|x86_64)-(?:opt|fastbuild|dbg)(?:-exec)?(?:-ST-[a-f0-9]{6,64})?)/bin/")
VERSION = re.compile(rb"(?:bb983b988802|0[.]0[.]0|release-[0-9]{8}[.][0-9]+)")
SOURCE = "bb983b988802b854bd03dd7a0a5220e7e7b4440e"


def sha(contents):
    return hashlib.sha256(contents).hexdigest()


def sections(binary):
    assert binary[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", binary, 18)[0] == 183
    table = struct.unpack_from("<Q", binary, 40)[0]
    size, count, strings = struct.unpack_from("<HHH", binary, 58)
    assert size == 64 and 0 < strings < count <= 256 and table + count * size <= len(binary)
    rows = [struct.unpack_from("<IIQQQQIIQQ", binary, table + i * size) for i in range(count)]
    off, n = rows[strings][4:6]
    assert off + n <= len(binary)
    names = binary[off:off+n]
    result = {}
    for idx, (nameoff, kind, flags, address, offset, length, link, _, _, entsize) in enumerate(rows[1:], 1):
        assert nameoff < n
        end = names.index(0, nameoff)
        name = names[nameoff:end].decode("ascii")
        if kind == 8:  # SHT_NOBITS contributes memory, but has no bytes in the file.
            data = b""
        else:
            assert offset + length <= len(binary)
            data = binary[offset:offset+length]
        assert name not in result
        result[name] = (data, length, flags, address, idx, link, entsize)
    return result


def workspace_stamp(parts):
    if ".symtab" not in parts:
        return {"state": "symbols-unavailable"}
    symbols, _, _, _, _, link, width = parts[".symtab"]
    strings, = [v[0] for v in parts.values() if v[4] == link]
    assert width == 24
    for p, _, _, _, address, size in struct.iter_unpack("<IBBHQQ", symbols):
        if strings[p:strings.index(0, p)] != b"gvisor.dev/gvisor/runsc/version.version" or size != 16:
            continue
        def span(va, n):
            return next((v[0][va-v[3]:va-v[3]+n] for v in parts.values()
                         if v[3] and v[3] <= va and va+n <= v[3]+len(v[0])), None)
        header = span(address, 16)
        if header:
            va, n = struct.unpack("<QQ", header)
            raw = span(va, n) if 0 < n <= 96 else None
            if raw:
                return {"state": "read", "sha256": sha(raw),
                        "publicValue": raw.decode() if VERSION.fullmatch(raw) else None}
    return {"state": "symbol-unavailable"}


def file_table(parts):
    for name in (".gopclntab", ".data.rel.ro.gopclntab"):
        if name in parts:
            raw = parts[name][0]
            break
    else:
        return []
    if len(raw) < 72 or raw[:4] != b"\xf1\xff\xff\xff" or raw[7] != 8:
        return []
    _, files, _, _, _, start, end, _ = struct.unpack_from("<8Q", raw, 8)
    if not (0 < files < 1000000 and 72 <= start < end <= len(raw)):
        return []
    names = raw[start:end].split(b"\0", files)
    return names[:files] if len(names)>files else []


def buildinfo(path):
    # The Go CLI reads the file without executing it. Never persist its raw output.
    proc = subprocess.run(["go", "version", "-m", path], capture_output=True, env={**os.environ,"GOTOOLCHAIN":"local"})
    lines = proc.stdout.splitlines()
    if proc.returncode or not lines:
        return {"exit": proc.returncode}, {}
    version = re.search(rb"\b(go1[.][0-9]+(?:[.][0-9]+)?)$", lines[0])
    values = {}
    for line in lines[1:]:
        fields = line.strip().split(b"\t")
        if len(fields) == 2 and fields[0] == b"build":
            k, _, v = fields[1].partition(b"=")
            values[k] = v
    public = {b"GOOS":{b"linux"}, b"GOARCH":{b"arm64"}, b"GOARM64":{b"v8.0",b"v8.1"},
              b"CGO_ENABLED":{b"0",b"1"},b"-compiler":{b"gc"},b"-buildmode":{b"exe",b"pie"}}
    return {"exit": proc.returncode, "go": version[1].decode() if version else None,
            "metadataSha256":sha(b"\n".join(lines[1:])),
            "settings":{k.decode():v.decode() for k,v in values.items() if v in public.get(k,set())}}, values


def file_paths(tables):
    def configs(names):
        return collections.Counter(hit[1].decode() for name in names for hit in CONFIG.finditer(name))
    def normalize(path):
        return CONFIG.sub(b"/bazel-out/CONFIG/bin/", path)
    def chain(names):
        return sha(b"\x00".join(names))
    direct, paired = tables
    a, b = [list(map(normalize, names)) for names in tables]
    repo_dirs = {path.rsplit(b"/",1)[0] for path in subprocess.check_output(
        ["git","ls-tree","-r","--name-only",SOURCE,"--","pkg","runsc","tools"]).splitlines() if path.endswith(b"/BUILD")}
    examples = []
    old_by_name = {normalize(p):p for p in direct if CONFIG.search(p)}
    example_keys = set()
    for current in paired:
        old = old_by_name.get(normalize(current))
        if old is None or current == old:
            continue
        old_hit, new_hit = CONFIG.search(old), CONFIG.search(current)
        if not old_hit or not new_hit:
            continue
        tail = current[new_hit.end():].split(b"/")
        package = b"/".join(tail[:-1])
        if package not in repo_dirs or package in example_keys:
            continue
        example_keys.add(package)
        examples.append({"publicPackage":package.decode(),"oldConfig":old_hit[1].decode(),
                         "newConfig":new_hit[1].decode(),"matchedPublicSuffixSha256":sha(b"/".join(tail))})
        if len(examples) == 6:
            break
    return {"counts":[len(direct),len(paired)],"configs":[configs(direct),configs(paired)],
            "orderedDigest":[chain(direct),chain(paired)],"normalizedOrderedDigest":[chain(a),chain(b)],
            "sameMultisetAfterConfigNormalization":collections.Counter(a)==collections.Counter(b),
            "rawEqual":direct==paired,"examples":examples}


def main():
    result = {"source":SOURCE,"nativeARM64":os.uname().machine=="aarch64","verified":False}
    assert result["nativeARM64"]
    rows, settings, parts = {}, [], []
    for name, (_, checksum, size) in FILES.items():
        path = ROOT / name
        raw = path.read_bytes()
        assert len(raw)==size and sha(raw)==checksum
        sections_by_name = sections(raw)
        parts.append(sections_by_name)
        row, values = buildinfo(path)
        rows[name] = {"size":size,"sha256":checksum,"workspaceVersion":workspace_stamp(sections_by_name),"go":row,
                      "goBuildIdHasRedactedMarker":b"redacted" in sections_by_name.get(".note.go.buildid", (b"",))[0]}
        settings.append(values)
    result["files"]=rows
    result["settingsMatch"]=settings[0]==settings[1] if all(r["go"].get("go") for r in rows.values()) else None
    compared = []
    for key in sorted(set(parts[0])|set(parts[1])):
        pair = [p.get(key) for p in parts]
        label = key if key in PUBLIC_SECTIONS else "section-sha256:"+sha(key.encode())[:16]
        compared.append({"section":label,"executable":any(p and p[2]&4 for p in pair),
            "loaded":any(p and p[2]&2 for p in pair),
            "bytes":[p[1] if p else None for p in pair],
            "sha256":[sha(p[0]) if p else None for p in pair]})
    result["sections"]=compared
    (ROOT/"comparison.json").write_text(json.dumps(result,indent=2)+"\n")
    tables = [file_table(p) for p in parts]
    result["goFilePaths"] = file_paths(tables) if all(tables) else {"available":False}
    result["verified"]=True
    (ROOT/"comparison.json").write_text(json.dumps(result,indent=2)+"\n")


if __name__=="__main__":
    main()
