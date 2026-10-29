"""Exercise the actual projection before initiating Bazel work."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

report = Path(__file__).with_name("retention_report.py").resolve()
owner = "//pkg/state:state_test"
suite = "TestGraphScratchLifetime"
with TemporaryDirectory() as name:
    root = Path(name)
    for mode in ("stock","fixed","missing"):
        stock = mode=="stock"
        rows = [suite]+[suite+"/"+n for n in ("reuse_plain","fresh_alias","reuse_alias","pool_only")]
        if mode=="missing": rows.pop()
        xml=ET.Element("testsuite")
        for r in rows:
            node=ET.SubElement(xml,"testcase",name=r)
            if stock and r in (suite,suite+"/reuse_alias",suite+"/pool_only"): ET.SubElement(node,"failure")
        flag="true" if stock else "false"
        log=(f"SCRATCH roundtrip=1 reused=true self=false len=1 cap=1 stale=false alive=false after_clear=false\n"
             f"SCRATCH roundtrip=1 reused=false self=true len=0 cap=0 stale=false alive=false after_clear=false\n"
             f"SCRATCH roundtrip=1 reused=true self=true len=0 cap=1 stale={flag} alive={flag} after_clear=false\n"
             f"POOL roundtrip=1 cache_first=true payload_first={flag} cache_second=false payload_second=false\n").encode()
        data={"test.log":log,"test.xml":ET.tostring(xml)}
        test=root/"bazel-testlogs/pkg/state/state_test"
        test.mkdir(parents=True,exist_ok=True)
        for f,v in data.items(): (test/f).write_bytes(v)
        tr=dict(status="FAILED" if stock else "PASSED",executionInfo={"strategy":"remote"},testActionOutput=[{"name":k,"digest":sha256(v).hexdigest(),"length":len(v)} for k,v in data.items()])
        events=[{"started":{"uuid":"fixture"}},{"id":{"testResult":{"label":owner}},"testResult":tr},{"finished":{"exitCode":{"code":3 if stock else 0}}}]
        (root/"events.jsonl").write_text("".join(json.dumps(e)+"\n" for e in events))
        ret=subprocess.run([sys.executable,"-B",report,root,mode,"fixture",owner,"1" if stock else "0"],cwd=root).returncode
        assert ret==int(mode=="missing")
