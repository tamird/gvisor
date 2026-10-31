import re, struct, subprocess

def cmd(*args):
    return subprocess.check_output(args, text=True).strip()

def notes(exe):
    dump = cmd("readelf", "--hex-dump=.note.go.buildid", "--wide", str(exe))
    words = []
    for line in dump.splitlines():
        if m := re.match(r"\s*0x[0-9a-f]+\s+((?:[0-9a-f]{8}(?:\s+|$)){1,4})", line):
            words.extend(m[1].split())
    data = bytes.fromhex("".join(words))
    namesz, size, typ = struct.unpack_from("<III", data)
    assert data[12:12+namesz].rstrip(b"\0")==b"Go" and typ==4
    pos=12+(namesz+3)//4*4
    go=data[pos:pos+size].decode()
    assert re.fullmatch(r"[0-9a-f]{20}(?:/[0-9a-f]{20}){3}",go),go
    gnu=re.findall(r"Build ID: ([0-9a-f]+)",cmd("readelf","--notes","--wide",str(exe)))
    assert len(gnu)==1 and len(gnu[0])>=32,gnu
    raw=go.replace("/","")
    assert raw[:len(gnu[0])]==gnu[0] and raw[-16:]==raw[:16] and set(raw)!={"0"}, (go,gnu)
    return go,gnu[0]
