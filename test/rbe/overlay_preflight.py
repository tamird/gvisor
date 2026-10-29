"""Exercise the actual projection before initiating Bazel work."""
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

report = Path(__file__).with_name("overlay_report.py").resolve()
with TemporaryDirectory() as name:
    root = Path(name)
    for sample in ("fixed", "waiter_pass", "waiter_fail", "missing"):
        mode = "fixed" if sample == "fixed" else "waiter"
        stock = sample == "waiter_fail"
        owner = "//pkg/sentry/fsimpl/proc:proc_test" if mode == "waiter" else "//pkg/sentry/fsimpl/overlay:overlay_test"
        case = "TestOverlayPollUnregisterAfterCopyUp"
        suite = ET.Element("testsuite")
        msg = "hostname change woke epoll after its interest was deleted" if stock else ""
        cases = {case, case+"/same_lower", case+"/copied_to_tmpfs"} if mode == "waiter" else {"TestOnCloseDuringCopyUp"}
        if sample == "missing":
            cases.discard(case+"/same_lower")
        for name in cases:
            node = ET.SubElement(suite, "testcase", name=name)
            if stock and name != case+"/same_lower":
                ET.SubElement(node,"failure").text = msg
        data = {"test.log": msg.encode(), "test.xml": ET.tostring(suite)}
        test = root/"bazel-testlogs"/owner[2:].replace(":", "/")
        test.mkdir(parents=True, exist_ok=True)
        for k,v in data.items():
            (test/k).write_bytes(v)
        tr = dict(status="FAILED" if stock else "PASSED", executionInfo={"strategy":"remote"}, testActionOutput=[{"name":k,"digest":sha256(v).hexdigest(),"length":len(v)} for k,v in data.items()])
        events = [{"started":{"uuid":"fixture"}},{"id":{"testResult":{"label":owner}},"testResult":tr},{"finished":{"exitCode":{"code":3 if stock else 0}}}]
        (root/"events.jsonl").write_text("".join(json.dumps(e)+"\n" for e in events))
        code = subprocess.run([sys.executable,"-B",report,root,mode,"fixture",owner,"1" if stock else "0"],cwd=root).returncode
        assert code == int(sample == "missing"), (mode, code)
