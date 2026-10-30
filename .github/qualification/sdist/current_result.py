"""Check the archive action from the real, independently assembled product."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tarfile

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
    assert set(files.values())=={
        "45abbeef0911c688909a3f84ba17f9ca6f409866fad3546f3aa230eee5217ae9",
        "863afdff6305bfd5d635755f35613f63b9ee9b88d68ac7517f6e314201944ba8"}
    archive,=dist.glob("*.tar.gz")
    with tarfile.open(archive) as f:
        assert len(f.getmembers())==13 and all(x.mtime==315532800 for x in f)
    entry["status"]="PASS"
finally:
    (out/f"current-{lane}.json").write_text(json.dumps(entry,indent=2)+"\n")
