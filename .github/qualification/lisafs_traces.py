"""Decode the two execution traces archived by one ordinary systrap syscall test."""

import gzip
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from zipfile import ZipFile

import native
import previous


CASE = "UnameTest.Sanity"
LABEL = "//test/syscalls:uname_test_runsc_systrap_directfs"
native.SYSCALLS[LABEL] = [CASE]
native.REPORT = {"source": os.environ["GITHUB_SHA"], "tests": {}, "commands": {}, "inputs": {}}
previous.OUT = native.WORK
previous.REPORT["assets"] = native.REPORT["inputs"]
try:
    native.run("uname", [LABEL], ["--test_env=GTEST_FILTER=" + CASE, "--zip_undeclared_test_outputs"], 100)
finally:
    for log in native.WORK.glob("uname-*.console"):
        (native.OUT / (log.name + ".tail")).write_bytes(log.read_bytes()[-24000:])
    native.save()
test = native.REPORT["tests"][LABEL]
assert test["verified"], test
testdir = Path("bazel-testlogs/test/syscalls/uname_test_runsc_systrap_directfs")
archive = native.OUT / "outputs.zip"
shutil.copyfile(testdir / "test.outputs/outputs.zip", archive)

output_base = subprocess.check_output([
    str(native.WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g",
    "info", "output_base"
], text=True).strip()
sdk = sorted((Path(output_base) / "external").glob("*go_sdk*/bin/go"))
go = next(path for path in sdk if "go1.27.3" in subprocess.check_output([path, "version"], text=True))
native.REPORT["go"] = subprocess.check_output([go, "version"], text=True).strip()

with ZipFile(archive) as z:
    names = z.namelist()
    sentry, = [name for name in names if re.fullmatch(r"runtime-[^/]+\.trace", name)]
    gofer = sentry + ".gofer"
    assert gofer in names and len([n for n in names if n.startswith("runtime-")]) == 2, names
    for role, member in (("sentry", sentry), ("gofer", gofer)):
        raw = z.read(member)
        path = native.OUT / (role + ".trace")
        path.write_bytes(raw)
        row = dict(archive_path=member, bytes=len(raw), sha256=native.sha(raw))
        native.REPORT[role] = row
        native.save()
        assert raw.startswith(b"go 1.") and b"trace" in raw[:16], (role, raw[:16])

        # Decode the archived file through EOF. Keep both successes and decoder
        # errors even if one role stops before Go can flush its final generation.
        profile = native.OUT / (role + "-scheduler.pb.gz")
        with profile.open("wb") as out:
            result = subprocess.run([go, "tool", "trace", "-pprof=sched", path], stdout=out, stderr=subprocess.PIPE)
        row["decoder_exit"] = result.returncode
        row["decoder_stderr"] = result.stderr.decode(errors="replace")[-3000:]
        native.save()
        if result.returncode:
            continue
        assert gzip.decompress(profile.read_bytes()), role
        report = subprocess.check_output([go, "tool", "pprof", "-raw", profile], text=True)
        (native.OUT / (role + "-scheduler.txt")).write_text(report)
        row["scheduler_samples"] = any(re.match(r"\s+\d+(?:\s+\d+)*:\s+\d", l) for l in report.splitlines())
        row["lisafs_frames"] = sorted({l.strip() for l in report.splitlines() if "/lisafs." in l})[:20]

native.REPORT["verified"] = all(native.REPORT[r]["decoder_exit"] == 0 for r in ("sentry", "gofer"))
native.save()
print(json.dumps(native.REPORT, indent=2))
assert native.REPORT["verified"], "One or both archived execution traces failed to decode."
