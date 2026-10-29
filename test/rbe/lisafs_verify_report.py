"""Exercise the actual projection before initiating Bazel work."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

report = Path(__file__).with_name("lisafs_tmp_report.py").resolve()
owner = "//runsc/gofer/fs:lisafs_test"
with TemporaryDirectory() as name:
    root = Path(name)
    for mode in ("new-bazel", "new-empty", "new-unset"):
        good = mode != "new-empty"
        rows = ["TestFSGofer","TestFSGofer/passthrough","TestFSGofer/virtual"]
        if good:
            rows += ["TestFSGofer/passthrough/UDS","TestFSGofer/virtual/UDS"]
        if mode == "new-unset":
            rows.pop()
        suite = ET.Element("testsuite")
        for r in rows:
            case = ET.SubElement(suite,"testcase",name=r)
            if not good:
                ET.SubElement(case,"failure")
        test = root / "bazel-testlogs/runsc/gofer/fs/lisafs_test"
        test.mkdir(parents=True, exist_ok=True)
        data = {"test.log": b"" if good else b"opening temporary directory failed: open : no such file or directory\n"*2, "test.xml": ET.tostring(suite)}
        for leaf,v in data.items():
            (test/leaf).write_bytes(v)
        tr = dict(status="PASSED" if good else "FAILED", executionInfo={"strategy":"remote"}, testActionOutput=[{"name":k,"digest":sha256(v).hexdigest(),"length":str(len(v))} for k,v in data.items()])
        events = [{"started":{"uuid":"fixture"}},{"id":{"testResult":{"label":owner}},"testResult":tr},{"finished":{"exitCode":{"code":0 if good else 3}}}]
        (root/"events.jsonl").write_text("".join(json.dumps(e)+"\n" for e in events))
        ret=subprocess.run([sys.executable,"-B",report,root,mode,"fixture",owner,"0" if good else "1"],cwd=root).returncode
        assert ret == int(mode=="new-unset")
