"""Exercise original and revised callers on one worker, then the unmodified bridge cases."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import struct
import time

import native
import previous

WORK = native.WORK
OUT = native.OUT
IMAGE = "//test/image:image_test"
E2E = "//test/e2e:integration_test"
native.REPORT = {"source": "b37e519bbf5d14868442a32f336f768d955820b3", "parent":"dd49ce763bea6f5b38eff62f2ca586b6a22c4a9f",
                 "commands": {}, "tests": {}, "inputs": {}, "errors": []}
previous.OUT = WORK
previous.REPORT["assets"] = native.REPORT["inputs"]
native.GO[IMAGE] = ["TestQualificationStalledPeer/old", "TestQualificationStalledPeer/revised",
                    "TestQualificationDockerStreams/old", "TestQualificationDockerStreams/revised"]
native.GO[E2E] = ["TestExecCancellation", "TestExecError"]
verbose = ["--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1"]
def run():
    assert os.uname().machine == "aarch64"
    native.FLAGS = ["--config=aarch64" if flag == "--config=x86_64" else flag for flag in native.FLAGS]
    native.REPORT["worker"] = os.uname().machine
    old = subprocess.check_output(["git","show", native.REPORT["parent"] + ":test/image/image_test.go"],text=True)
    start = old.index("func dockerInGvisorExecOutput(")
    fn = old[start:old.index("\n}",start)+2].replace("func dockerInGvisorExecOutput(","func savedDockerExec(")
    assert fn in Path("test/image/deadline_qualification_test.go").read_text()
    native.run("controls", [IMAGE], verbose + ["--test_arg=-test.run=^TestQualification", "--test_env=RUNTIME=runc"], 70)
    controls = native.REPORT["tests"].pop(IMAGE)
    native.REPORT["controls"] = controls
    for k in ("test.xml","test.log"):
        path = OUT / controls[k]["file"]
        renamed = path.with_name("controls-" + path.name)
        path.rename(renamed)
        controls[k]["file"] = renamed.name
    native.save()
    # Use the upstream Make docker-tests configuration for the two nested Docker cases.
    release = WORK / "release"
    shutil.copytree("bazel-bin/release",release,symlinks=False)
    binary = str(release/"runsc")
    native.REPORT["runsc_sha256"] = native.sha((release/"runsc").read_bytes())
    with open(binary,"rb") as elf:
        header = elf.read(20)
    assert header[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H",header,18)[0] == 183
    native.REPORT["runtime_elf_machine"] = 183
    subprocess.run(["sudo","-n",binary,"install","--runtime=runsc-dind-check"],check=True)
    subprocess.run(["sudo","-n",binary,"install","--runtime=runsc-dind-check-docker","--",
                    "--net-raw","--allow-packet-socket-write"],check=True)
    subprocess.run(["sudo","-n","systemctl","reload","docker"],check=True)
    for _ in range(30):
        runtimes = json.loads(subprocess.check_output(["docker","info","--format","{{json .Runtimes}}"],text=True))
        if {"runsc-dind-check", "runsc-dind-check-docker"}.issubset(runtimes): break
        time.sleep(1)
    else: raise RuntimeError("named Docker runtime unavailable")
    native.GO[IMAGE]=[f"TestDockerWith{name}/docker_run_bridge_network_non_privileged" for name in ("VFS","Overlay")]
    native.run("bridge-and-exec",[IMAGE,E2E],verbose+["--test_env=RUNTIME=runsc-dind-check",
               "--test_arg=-test.run=^(TestExecError|TestExecCancellation|TestDockerWith(VFS|Overlay))$/^docker_run_bridge_network_non_privileged$",
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
