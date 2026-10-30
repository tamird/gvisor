"""Check the archive action from the real, independently assembled product."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import zipfile

lane=sys.argv[1]
data,out=map(Path,sys.argv[2:])
sys.path.insert(0,str(Path(os.environ["RUNNER_TEMP"])/"sdist-fixture"))
from qualification_report import compact_records

entry={"source":os.environ["CURRENT_PRODUCT"],"lane":lane,"status":"FAIL"}
try:
    status=int((out/f"current-{lane}.exit").read_text())
    entry["native_exit"]=status
    lines=(data/"private.log").read_text(errors="replace").splitlines()
    entry["diagnostics"]=[row[:350] for row in lines if re.search("Error in |ImportError|error: |ERROR: |Caused by|Traceback|not found|No such file",row,re.I) and not re.search("token|https?://|credential|secret|header|api.key",row,re.I)][:12]
    events=[json.loads(line) for line in (data/"events").read_text().splitlines()]
    entry["invocation"]=next(e["started"]["uuid"] for e in events if "started" in e)
    if (data/"spawns").is_file():
        _,spawns=compact_records(data/"spawns")
        entry["actions"]=[{k:s.get(k) for k in ("mnemonic","targetLabel","platform","runner","cacheHit","exitCode","status")}
                    for s in spawns if s.get("mnemonic")=="PythonDistribution"]
    assert status==0 and len(entry["actions"])==1,entry.get("native_exit")
    action=entry["actions"][0]
    assert not action.get("cacheHit") and not action.get("exitCode")
    if lane=="scratch":
        assert action["runner"]=="remote"
        assert any("hello-world@sha256:c567" in p.get("value","") for p in action["platform"]["properties"])
    dist=Path("bazel-bin/sandboxexec/sandbox/python/dist")
    files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(dist.iterdir())}
    entry["files"]=files
    assert len(files)==2
    wheel,=dist.glob("*.whl")
    archive,=dist.glob("*.tar.gz")
    project=Path("sandboxexec/sandbox/python")
    pkg={f.relative_to(project).as_posix(): f.read_bytes() for f in sorted((project/"gvisor").glob("*.py"))}
    with zipfile.ZipFile(wheel) as z:
        assert all(z.read(name)==raw for name,raw in pkg.items())
        record,=[n for n in z.namelist() if n.endswith(".dist-info/RECORD")]
        assert z.read(record) and "gvisor/sandbox.py" in z.namelist()
    with tarfile.open(archive) as f:
        assert len(f.getmembers())==13 and all(x.mtime==315532800 for x in f)
        all_payload={**pkg,"README.md":(project/"README.md").read_bytes()}
        prefix="gvisor-2026.10.29.2/"
        assert all(f.extractfile(prefix+name).read()==raw for name,raw in all_payload.items())
    entry["source_members"]=sorted(all_payload)
    entry["sdist_members"]=13
    entry["gzip_mtime"]=int.from_bytes(archive.read_bytes()[4:8],"little")
    assert entry["gzip_mtime"]==315532800
    entry["status"]="PASS"
finally:
    (out/f"current-{lane}.json").write_text(json.dumps(entry,indent=2)+"\n")
