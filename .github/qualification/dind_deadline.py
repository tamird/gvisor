"""Exercise original and revised callers on one worker, then the unmodified bridge cases."""
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time

import native
import previous

WORK = native.WORK
OUT = native.OUT
IMAGE = "//test/image:image_test"
E2E = "//test/e2e:integration_test"
native.REPORT = {"source": "4cd80a062199ce7ddab3e8e73e6e05900a09f530", "stock": "b37e519bbf5d14868442a32f336f768d955820b3", "parent":"dd49ce763bea6f5b38eff62f2ca586b6a22c4a9f",
                 "commands": {}, "tests": {}, "inputs": {}, "errors": []}
previous.OUT = WORK
previous.REPORT["assets"] = native.REPORT["inputs"]
native.GO[IMAGE] = ["TestQualificationStalledPeer/old", "TestQualificationStalledPeer/revised",
                    "TestQualificationDockerStreams/old", "TestQualificationDockerStreams/revised"]
native.GO[E2E] = ["TestExecCancellation"]
verbose = ["--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1"]
def archive(label, name):
    record = native.REPORT["tests"].pop(label)
    native.REPORT[name] = record
    for key in ("test.xml", "test.log"):
        path = OUT / record[key]["file"]
        dst = path.with_name(name + "-" + path.name)
        path.rename(dst)
        record[key]["file"] = dst.name
    native.save()
    return record


def run():
    stock = native.REPORT["stock"]
    candidate = native.REPORT["source"]
    helper = "pkg/test/dockerutil/exec.go"
    def source(ref):
        return subprocess.check_output(["git", "show", ref + ":" + helper])
    actual = Path(helper)
    assert subprocess.check_output(["git", "diff", "--name-only", stock, candidate], text=True).splitlines() == [helper]
    assert actual.read_bytes() == source(candidate)
    native.REPORT["helper_sha256"] = {r: native.sha(source(r)) for r in (stock, candidate)}
    modes = {"systemerr", "nonzero", "cancel", "deadline"}
    prefix = "TestQualificationPartialOutput"
    fixed_controls = list(native.GO[IMAGE]) + [prefix + "/" + m for m in sorted(modes)]
    fixed_controls += ["TestQualificationNativePartial/" + m for m in ("cancel", "deadline")]
    native.GO[IMAGE] = [prefix + "/" + m for m in sorted(modes)]
    try:
        actual.write_bytes(source(stock))
        native.run("stock", [IMAGE], verbose + ["--test_env=RUNTIME=runc","--test_arg=-test.run=^" + prefix + "$"], 70)
        row = archive(IMAGE, "stock-loss")
        text = (OUT / row["test.log"]["file"]).read_text()
        lost = re.findall(r'PARTIAL_OUTPUT_LOST mode=(\w+) got="" error=', text)
        unexpected = set(row["failing_cases"]) - {prefix} - set(native.GO[IMAGE])
        assert not unexpected and set(native.GO[IMAGE]).issubset(row["failing_cases"]), row["failing_cases"]
        assert row["status"] == "FAILED" and row["strategy"] in {"standalone", "local"} and not row["cached"]
        assert set(lost) == modes, lost
        row["observed_loss"] = sorted(set(lost))
    finally:
        actual.write_bytes(source(candidate))
    assert subprocess.run(["git", "diff", "--exit-code", "--", helper]).returncode == 0
    native.GO[IMAGE] = fixed_controls
    native.run("controls", [IMAGE,E2E], verbose + ["--test_arg=-test.run=^(TestQualification.*|TestExecCancellation)$", "--test_env=RUNTIME=runc"], 40)
    controls = archive(IMAGE, "controls")
    # Use the upstream Make docker-tests configuration for the two nested Docker cases.
    release = WORK / "release"
    shutil.copytree("bazel-bin/release",release,symlinks=False)
    binary = str(release/"runsc")
    native.REPORT["runsc_sha256"] = native.sha((release/"runsc").read_bytes())
    subprocess.run(["sudo","-n",binary,"install","--runtime=dind-check-docker","--",
                    "--net-raw","--allow-packet-socket-write"],check=True)
    subprocess.run(["sudo","-n","systemctl","reload","docker"],check=True)
    for _ in range(30):
        runtimes = json.loads(subprocess.check_output(["docker","info","--format","{{json .Runtimes}}"],text=True))
        if "dind-check-docker" in runtimes: break
        time.sleep(1)
    else: raise RuntimeError("named Docker runtime unavailable")
    native.GO[IMAGE]=[f"TestDockerWith{name}/docker_run_bridge_network_non_privileged" for name in ("VFS","Overlay")]
    native.run("bridge",[IMAGE],verbose+["--test_env=RUNTIME=dind-check",
               "--test_arg=-test.run=^TestDockerWith(VFS|Overlay)$/^docker_run_bridge_network_non_privileged$",
               "--test_timeout=900"],35)
    for row in [controls, native.REPORT["tests"][E2E], native.REPORT["tests"][IMAGE]]:
        assert row.get("verified") and not row["cached"] and row["attempt"]==1 and row["skipped_count"]==0, row

try:
    run()
except Exception as error:
    native.REPORT["errors"].append(repr(error)[-5500:])
native.REPORT["verified"] = not native.REPORT["errors"]
native.save()
if not native.REPORT["verified"]:
    sys.exit(str(native.REPORT["errors"]))
