"""Verify the fixed native and guest ABI and published Docker owner tests."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from xml.etree import ElementTree as ET

import native
import previous

WORK = native.WORK
OUT = native.OUT
CASES = ["Seize/PtraceAttachTest.AttachingConvertsGroupStopToPtraceStop/0",
         "Seize/PtraceAttachTest.AttachingConvertsGroupStopToPtraceStop/1",
         "PtraceTest.Seize_Interrupt_Listen"]
SYSCALLS = ["//test/syscalls:ptrace_test_" + plat for plat in ("native", "runsc_systrap_directfs")]
GO = {"//test/e2e:integration_test": ["TestSudo"], "//test/image:image_test": ["TestGDBAttach"]}
SMOKE = "//test/qualify-corrected-debugger:benign_trace_test"

native.REPORT = {"source": "71bdb92251810c03229d67d0e3bdf2a9c9cba9d2", "commands": {}, "tests": {}, "inputs": {}, "errors": []}
previous.OUT = WORK
previous.REPORT["assets"] = native.REPORT["inputs"]
native.SYSCALLS.update(dict.fromkeys(SYSCALLS, CASES))
native.GO.update(GO)
def command(args):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE).stdout

def release():
    flags = [str(WORK/"bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g", "build",
             "--config=x86_64", "--jobs=4", "--lockfile_mode=off", "--remote_executor=", "--remote_cache=",
             "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local",
             "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox"]
    for ix in range(20):
        path = WORK / f"release-{ix}.log"
        with path.open("w+") as log:
            rc = subprocess.run([*flags, *["--distdir=" + str(d) for d in sorted(previous.DIRS)], "//:release"],
                                stdout=log, stderr=subprocess.STDOUT).returncode
            log.seek(0)
            text = log.read()
        if rc == 0:
            break
        if not previous.recover(text):
            raise RuntimeError(text[-4000:])
    else:
        raise RuntimeError("release dependency limit")
    release_dir = WORK/"release"
    shutil.copytree("bazel-bin/release",release_dir,symlinks=False)
    native.REPORT["release"] = {str(p.relative_to(release_dir)):hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in release_dir.rglob("*") if p.is_file()}
    assert all(k in native.REPORT["release"] for k in ("runsc", "gvisor-bin/gvisor_sentry"))
    logfile = WORK/"runtime-logs"
    logfile.mkdir()
    command(["sudo", "-n", str(release_dir/"runsc"), "install", "--runtime=corrected", "--experimental=true", "--",
             "--debug-log", str(logfile/"runsc.%TEST%.%TIMESTAMP%.%COMMAND%"),
             "--allow-suid", "--TESTONLY-test-name-env=RUNSC_TEST_NAME"])
    command(["sudo", "-n", "systemctl", "reload", "docker"])
    for _ in range(20):
        if "corrected" in json.loads(command(["docker","info","--format","{{json .Runtimes}}"])):
            break
        time.sleep(1)
    else:
        raise RuntimeError("corrected Docker runtime unavailable")
    native.save()

def run_tests():
    return native.run("abi",SYSCALLS,["--test_env=GTEST_FILTER=" + ":".join(CASES)],65)

def docker_tests():
    release()
    native.run("docker",list(GO),["--test_env=RUNTIME=corrected","--test_arg=-test.v","--test_env=GO_TEST_WRAP_TESTV=1",
               "--test_arg=-test.run=^(TestSudo|TestGDBAttach)$"],45)
    native.GO[SMOKE]=[]
    native.run("ordinary",[SMOKE],[],10)
    row=native.REPORT["tests"][SMOKE]
    log=(OUT/row["test.log"]["file"]).read_text()
    assert '+++ exited with 0 +++' in log and row["verified"],row

uses = {"syscalls":run_tests,"docker":docker_tests}
for name, action in uses.items():
    try:
        action()
    except Exception as exc:
        native.REPORT["errors"].append({"stage":name,"error":str(exc)[-4000:]})
    native.save()
for label in [*SYSCALLS,*GO,SMOKE]:
    row=native.REPORT["tests"].get(label,{})
    if not row.get("verified") or row.get("cached") or row.get("attempt")!=1 or row.get("status")!="PASSED":
        native.REPORT["errors"].append({"test":label, "status":row.get("status"),"verification":row.get("error")})
native.REPORT["verified"]=not native.REPORT["errors"]
native.save()
if not native.REPORT["verified"]:
    sys.exit(str(native.REPORT["errors"])[-4600:])
