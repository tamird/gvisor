"""Exercise the actual projection before initiating Bazel work."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

report = Path(__file__).with_name("focused_report.py").resolve()
owner = "//tools/nogo/check:check_test"
with TemporaryDirectory() as name:
    root = Path(name)
    for index in range(3):
        mode="nogo"
        good = index != 1
        rows = ["TestExportDataInputs"] + ["TestExportDataInputs/"+n for n in ("direct","stdlib","missing","malformed","compiler_archive")]
        if index == 2:
            rows.pop()
        suite = ET.Element("testsuite")
        for r in rows:
            case = ET.SubElement(suite,"testcase",name=r)
            if not good:
                ET.SubElement(case,"failure")
        test = root / "bazel-testlogs/tools/nogo/check/check_test"
        test.mkdir(parents=True, exist_ok=True)
        data = {"test.log": b"" if good else b"synthetic negative", "test.xml": ET.tostring(suite)}
        for leaf,v in data.items():
            (test/leaf).write_bytes(v)
        tr = dict(status="PASSED" if good else "FAILED", executionInfo={"strategy":"remote"}, testActionOutput=[{"name":k,"digest":sha256(v).hexdigest(),"length":str(len(v))} for k,v in data.items()])
        events = [{"started":{"uuid":"fixture"}},{"id":{"testResult":{"label":owner}},"testResult":tr},{"finished":{"exitCode":{"code":0 if good else 3}}}]
        (root/"events.jsonl").write_text("".join(json.dumps(e)+"\n" for e in events))
        ret=subprocess.run([sys.executable,"-B",report,root,mode,"fixture",owner,"0" if good else "1"],cwd=root).returncode
        assert ret == int(index!=0)
