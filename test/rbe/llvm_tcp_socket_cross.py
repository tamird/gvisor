"""Qualify the existing TCP socket test as an ordinary hosted LLVM consumer."""
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import sys
import xml.etree.ElementTree as ET

import native_cross_inputs as q

SOURCE = "fffadb2bb858399110f8de18d04cbbf5e5c28997"
TARGET = "//test/syscalls/linux:tcp_socket_test"
PACKAGE = "test/syscalls/linux"
CPP = PACKAGE + "/tcp_socket.cc"
FLAGS = ["--config=aarch64", "-c", "opt", "--jobs=4", "--remote_cache=", "--remote_executor=", "--disk_cache="]
SUITE = "AllInetTests/TcpSocketTest"
NAMES = [SUITE + "." + test + "/" + str(i) for test in ("NoDelayDefault", "SetNoDelay") for i in (0, 1)]
TASK = Path(os.environ["RUNNER_TEMP"]) / "tcp-cross"
BAZEL = str(TASK / "bazel")
q.ROOTS = [TARGET]
q.owned = lambda label: label == TARGET


def dump(path, value):
    raw = json.dumps(value, sort_keys=True, indent=2).encode()
    assert len(raw) < 131072
    path.write_bytes(raw)


def options(args, key):
    return list(dict.fromkeys(args[i+1] if a == key else a[len(key):].lstrip("=") for i, a in enumerate(args) if a == key and i+1 < len(args) or a.startswith(key) and a != key))


def elf(path):
    raw = path.read_bytes()
    assert 64 < len(raw) < 100*1024*1024 and raw[:6] == b"\x7fELF\x02\x01"
    machine = struct.unpack_from("<H", raw, 18)[0]
    entry, table = struct.unpack_from("<QQ", raw, 24)
    width, count = struct.unpack_from("<HH", raw, 54)
    assert width >= 56 and table+width*count <= len(raw)
    segments, interp = [], None
    for i in range(count):
        idx = table + width*i
        t, _, off = struct.unpack_from("<IIQ", raw, idx)
        n, = struct.unpack_from("<Q", raw, idx+32)
        segments.append(t)
        if t == 3:
            assert 0 < n < 256 and off+n <= len(raw)
            interp = raw[off:off+n].rstrip(b"\0").decode()
    rc, out, err = q.command(["readelf", "-dW", str(path)], 15)
    assert rc == 0, q.diagnostics(err)
    names = re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", out.decode())
    assert len(names)<32 and all(re.fullmatch(r"[A-Za-z0-9_+.-]+", name) for name in names)
    return dict(bytes=len(raw), sha256=q.digest(raw), machine=machine, entry=entry, load=1 in segments, dynamic=2 in segments, interpreter=interp, needed=names)


def argument_rows(raw):
    graph = json.loads(raw)
    fragments = {int(v["id"]):v for v in graph["pathFragments"]}
    def path(i):
        v=fragments[int(i)]
        return (path(v["parentId"])+"/" if v.get("parentId") else "")+v["label"]
    artifacts={int(v["id"]):path(v["pathFragmentId"]) for v in graph["artifacts"]}
    owners={int(v["id"]):v["label"] for v in graph["targets"]}
    configs={int(v["id"]):v["checksum"] for v in graph["configuration"]}
    rows=[]
    for a in graph["actions"]:
        if owners[int(a["targetId"])] != TARGET or a["mnemonic"] not in ("CppCompile","CppLink"):
            continue
        argv=list(a["arguments"])+[v for p in a.get("paramFiles",[]) for v in p.get("arguments",[])]
        outputs=[artifacts[int(i)] for i in a["outputIds"]]
        if a["mnemonic"]=="CppCompile" and CPP not in argv:
            continue
        if a["mnemonic"]=="CppLink" and not any(p.endswith("/"+PACKAGE+"/tcp_socket_test") for p in outputs):
            continue
        triples={x.split("=",1)[1] for x in argv if x.startswith(("--target=","-target="))}
        triples.update(argv[i+1] for i,x in enumerate(argv[:-1]) if x in ("-target","--target"))
        rows.append(dict(mnemonic=a["mnemonic"],owner=TARGET,driver=argv[0],config=configs[int(a["configurationId"])],
                         outputs=outputs,argv_sha256=q.digest(json.dumps(argv).encode()),triples=sorted(triples),
                         includes=[x for x in options(argv,'-isystem') if any(v in x for v in ('kernel_headers','glibc','libcxx','googletest'))][:22],
                         sysroots=options(argv,'--sysroot'),library_search_dirs=options(argv,'-L')[:35],
                         linker=[x for x in argv if x.startswith(('-fuse-ld=','--ld-path='))][:4],
                         source=CPP if a["mnemonic"]=='CppCompile' else None))
    return rows


def producer(r, save):
    assert platform.machine()=="x86_64" and os.getuid()!=0
    q.source(SOURCE)
    assert "llvm-freestanding-runtimes.patch" not in Path("MODULE.bazel").read_text()
    bep=TASK/"build.bep"
    rc,_,err=q.command([BAZEL,"build",*FLAGS,"--verbose_failures","--build_event_publish_all_actions","--build_event_json_file="+str(bep),TARGET],3600)
    r.update(build_exit=rc,diagnostics=q.diagnostics(err),events=q.events(bep))
    if rc:r['status']='FAIL'
    save()
    rc,raw,err=q.command([BAZEL,"aquery",*FLAGS,"--output=jsonproto","--include_param_files",'mnemonic("CppCompile|CppLink", '+TARGET+')'],240)
    r.update(aquery_exit=rc,aquery_diagnostics=q.diagnostics(err))
    rows=[]
    if not rc:
        try:rows=argument_rows(raw)
        except (KeyError,ValueError) as ex:r["action_reader_error"]=type(ex).__name__
    r["actions"]=rows;save()
    ev=r["events"]
    for a in rows:
        a["completed"]=[x for x in ev["actions"] if x["owner"]==TARGET and x["type"]==a["mnemonic"] and x["configuration"]==a["config"] and x['output'] in a['outputs']]
    save()
    if r["build_exit"]:return
    rc,raw,err=q.command([BAZEL,"cquery",*FLAGS,"--output=files",TARGET],180)
    r.update(cquery_exit=rc,cquery_diagnostics=q.diagnostics(err));save()
    if rc:return
    path,=raw.decode().splitlines()
    rc,raw,err=q.command([BAZEL,"info","execution_root"],60)
    assert rc==0 and path.startswith("bazel-out/") and path.endswith("/"+PACKAGE+"/tcp_socket_test")
    root=Path(raw.decode().strip());binary=(root/path).resolve();assert binary.is_relative_to(root.resolve())
    meta=elf(binary);r["elf"]=meta;r["output"]=path;save()
    assert meta["machine"]==183 and meta["entry"] and meta["load"]
    root_target,=ev["targets"]
    actual_link,=[x for x in ev["actions"] if x["type"]=="CppLink" and x["owner"]==TARGET and x["success"] and x["exit"]==0 and x["output"]==path and x["configuration"]==root_target["configuration"]]
    assert root_target["success"] and ev["exit"]["name"]=="SUCCESS"
    h=TASK/"handoff";h.mkdir()
    shutil.copyfile(binary,h/"tcp_socket_test");assert elf(h/"tcp_socket_test")==meta
    dump(h/"identity.json",dict(run=r["run"],source=SOURCE,carrier=r['carrier'],target=TARGET,elf=meta))
    r["handoff_ready"]=True;save()
    assert len(rows)==2 and {x["mnemonic"] for x in rows}=={"CppCompile","CppLink"}
    assert len(ev["targets"])==1 and ev["targets"][0]["success"] and ev["exit"]["name"]=="SUCCESS" and not ev["aborted"] and not ev["test_events"]
    for a in rows:
        assert a["triples"]==["aarch64-linux-gnu"] and a["config"]==ev["targets"][0]["configuration"]
        assert len(a["completed"])==1 and a["completed"][0]['success'] and a["completed"][0]['exit']==0
        assert re.fullmatch(r"external/llvm\+\+llvm_toolchain_minimal\+llvm-toolchain-minimal-linux-amd64/bin/clang(?:\+\+)?|bazel-out/[^/]+/bin/external/llvm\+/tools/internal/link-wrapper-clang\+\+/link-wrapper-clang\+\+",a["driver"])
    cc,= [a for a in rows if a['mnemonic']=='CppCompile']; ld,=[a for a in rows if a['mnemonic']=='CppLink']
    assert any('linux_kernel_headers_arm64' in p for p in cc['includes']) and any('glibc_headers_aarch64-linux-gnu' in p for p in cc['includes'])
    assert path in ld['outputs'] and any('glibc_library_search_directory' in p for p in ld['library_search_dirs'])
    r["status"]="PASS"


def consumer(r, save):
    assert platform.machine()=="aarch64" and os.getuid()!=0
    h=TASK/"handoff";assert {p.name for p in h.iterdir()}=={'tcp_socket_test','identity.json'} and all(p.is_file() and not p.is_symlink() for p in h.iterdir())
    ident=json.loads((h/'identity.json').read_text());binary=h/'tcp_socket_test';meta=elf(binary)
    assert ident==dict(run=r['run'],source=SOURCE,carrier=r['carrier'],target=TARGET,elf=meta) and meta['machine']==183 and meta['entry'] and meta['load']
    r['elf']=meta;save();binary.chmod(0o500)
    if meta["interpreter"]:
        loader=Path(meta["interpreter"])
        assert loader==Path('/lib/ld-linux-aarch64.so.1')
        r['loader_present']=loader.is_file();save()
        if loader.is_file():
            rc,out,err=q.command([str(loader),'--list',str(binary)],15)
            r.update(loader_list_exit=rc,loader_diagnostics=q.diagnostics(err),resolved_names=re.findall(r'^\s*([A-Za-z0-9_+.-]+)\s+=>\s+/',out.decode(errors='replace'),re.M))
            save()
    xml=TASK/"tcp.xml"
    rc,out,err=q.command([str(binary),"--gtest_filter="+":".join(NAMES),"--gtest_output=xml:"+str(xml)],120)
    r.update(native_exit=rc,stdout_sha256=q.digest(out),stderr_sha256=q.digest(err),diagnostics=q.diagnostics(err))
    if rc:r['status']='FAIL'
    save()
    if not xml.is_file():return
    raw=xml.read_bytes();assert 0 < len(raw)<65536
    r["xml_sha256"]=q.digest(raw);r["xml_bytes"]=len(raw);root=ET.fromstring(raw)
    cases=[dict(name=x.attrib["classname"]+"."+x.attrib["name"],status=x.get("status"),result=x.get("result"),fail=x.find('failure') is not None or x.find('error') is not None,skip=x.find('skipped') is not None) for x in root.findall('.//testcase')]
    r['cases']=cases;save()
    assert rc==0 and len(cases)==len(NAMES) and {c["name"] for c in cases}==set(NAMES)
    assert all(c['status']=='run' and c['result']=='completed' and not c['fail'] and not c['skip'] for c in cases)
    r['status']='PASS'


def main(phase):
    d=TASK/"public";d.mkdir(exist_ok=True)
    r=dict(status='INCOMPLETE',phase=phase,target=TARGET,source=SOURCE,carrier=os.environ['CARRIER_SHA'],run=int(os.environ['QUALIFICATION_RUN_ID']),worker=platform.machine(),flags=FLAGS if phase=='producer' else None,expected=NAMES)
    def save():dump(d/(phase+'.json'),r)
    save()
    try:
        {'producer':producer,'consumer':consumer}[phase](r,save)
    except (AssertionError,ValueError,KeyError,OSError,struct.error,ET.ParseError) as ex:
        r['error']=type(ex).__name__+': '+str(ex)[:340]
    save();return 0 if r['status']=='PASS' else 1


if __name__=="__main__":
    sys.exit(main(sys.argv[1]))
