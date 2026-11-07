#!/usr/bin/python3
"""Run the unchanged cmd owner in namespaces on a disposable native ARM VM."""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


OWNER = "//runsc/cmd:cmd_test"
MODES = ("default", "hostinet", "no_directfs", "no_directfs_hostinet", "fdlimit")
SIDECARS = {
    "checkpointgofer", "gvisor-proc-umounter", "gvisor-sentry-prewarmer",
    "gvisor_sentry", "runsc-fd-parking", "runsc-metric-server", "runsc-netgofer",
}
NAMESPACES = ("mnt", "pid", "net", "uts", "ipc")
PROOF = "HOSTED_CMD native ARM64; five private namespaces; seven matching release sidecars"
KERNEL = re.compile(r"[0-9][A-Za-z0-9._+-]{1,95}")


def namespace(kind):
    return os.readlink("/proc/self/ns/" + kind)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as data:
        for block in iter(lambda: data.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def wrapped(args):
    if os.getuid() == 0 or platform.machine() != "aarch64":
        return 2
    allowed = {"HOME", "LANG", "PATH", "CGROUPV2", "XML_OUTPUT_FILE", "GUNIT_OUTPUT",
               "JAVA_RUNFILES", "PYTHON_RUNFILES"}
    env = [f"{k}={v}" for k, v in os.environ.items()
           if k in allowed or k.startswith(("TEST_", "RUNFILES_", "GO_TEST_", "GTEST_"))]
    env.extend(f"PARENT_NS_{kind}={namespace(kind)}" for kind in NAMESPACES)
    os.execv("/usr/bin/sudo", ["sudo", "-n", "--", "/usr/bin/unshare", "--fork",
             "--mount", "--propagation", "private", "--mount-proc", "--pid", "--net",
             "--uts", "--ipc", "--", "/usr/bin/env", "-i", *env,
             str(Path(__file__).resolve()), "inside", *args])


def inside(args):
    if os.getuid() != 0 or any(namespace(k) == os.environ.pop("PARENT_NS_" + k)
                              for k in NAMESPACES):
        return 3
    source = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"] / "release/gvisor-bin"
    if {p.name for p in source.iterdir()} != SIDECARS:
        return 4
    # The no_directfs cases check every path component as uid 65534. Bazel's
    # private output base isn't traversable by that uid. Preserve the ELFs.
    with tempfile.TemporaryDirectory(prefix="hosted-cmd-sidecars-", dir="/tmp") as raw:
        target = Path(raw)
        target.chmod(0o755)
        for name in SIDECARS:
            src, dst = source / name, target / name
            with src.open("rb") as f:
                ident = f.read(20)
            if ident[:6] != b"\x7fELF\x02\x01" or int.from_bytes(ident[18:20], "little") != 183:
                return 5
            shutil.copyfile(src, dst)
            dst.chmod(0o755)
            if digest(src) != digest(dst):
                return 6
        os.environ["GVISOR_SIDECAR_BINARIES_DIR"] = raw
        print(PROOF, flush=True)
        return subprocess.run(args, check=False).returncode


def disposition(text):
    reasons = {
        "insufficient privilege": ("operation not permitted", "permission denied"),
        "unmapped user": ("UID 65534 is not mapped",),
        "unreachable Sentry": ("cannot be executed by an unprivileged user",),
        "missing file": ("no such file or directory",),
        "posture comparison": ("threads.uniform", "security posture mismatch"),
        "missing kernel feature": ("this kernel cannot report",),
    }
    return sorted(k for k, terms in reasons.items() if any(t.lower() in text.lower() for t in terms))


def report():
    root = Path(os.environ["RUNNER_TEMP"])
    release = platform.release()
    result = {"nativeArm": platform.machine() == "aarch64",
              "kernel": release if KERNEL.fullmatch(release) else "unknown",
              "fiveSentryLeaves": {}, "fiveGoferLeaves": {}}
    for phase in ("build", "local"):
        status = root / ("cmd-" + phase + ".exit")
        result[phase + "Exit"] = int(status.read_text()) if status.exists() else None
        log = root / ("cmd-" + phase + ".log")
        if log.exists():
            result[phase + "Signals"] = disposition(log.read_text(errors="replace"))
    bep = root / "cmd-local.bep"
    records = []
    if bep.exists():
        with bep.open() as events:
            for line in events:
                event = json.loads(line)
                ident = event.get("id", {}).get("testResult", {})
                if ident.get("label") == OWNER:
                    records.append(event["testResult"])
    result["testResults"] = len(records)
    result["freshStandalone"] = len(records) == 1 and not records[0].get("cachedLocally", False) and (
        records[0].get("executionInfo", {}).get("strategy") in ("standalone", "local")
    )
    base = Path("bazel-testlogs/runsc/cmd/cmd_test")
    code = 1
    log = base / "test.log"
    if records and log.is_file():
        content = log.read_text(errors="replace")
        result["hostedLauncherObserved"] = PROOF in content
        result["ownerLogSHA256"] = digest(log)
        result["ownerSignals"] = disposition(content)
        for match in re.finditer(r"(?m)^\s*--- (PASS|FAIL|SKIP): (TestBootPosture/[A-Za-z_]+/(?:sentry|gofer))\b", content):
            verdict, name = match.groups()
            _, mode, process = name.split("/")
            if mode in MODES:
                result[{"sentry": "fiveSentryLeaves", "gofer": "fiveGoferLeaves"}[process]][mode] = verdict
        xml = base / "test.xml"
        if xml.is_file():
            result["ownerXMLSHA256"] = digest(xml)
            counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
            for case in ET.parse(xml).iter("testcase"):
                name = case.get("name", "").removeprefix("cmd.")
                verdict = "FAIL" if any(case.find(n) is not None for n in ("failure", "error")) else (
                    "SKIP" if case.find("skipped") is not None else "PASS")
                counts[verdict] += 1
                if re.fullmatch(r"TestBootPosture/[A-Za-z_]+/(?:sentry|gofer)", name):
                    _, mode, process = name.split("/")
                    if mode in MODES:
                        leaves = result[{"sentry": "fiveSentryLeaves", "gofer": "fiveGoferLeaves"}[process]]
                        if leaves.get(mode, verdict) != verdict:
                            verdict = "CONFLICT"
                        leaves[mode] = verdict
            result["xmlCaseTotals"] = counts
        if (result["freshStandalone"] and result["hostedLauncherObserved"] and result["nativeArm"]
                and result["localExit"] == result["buildExit"] == 0
                and result["fiveSentryLeaves"] == dict.fromkeys(MODES, "PASS")):
            code = 0
    result["passed"] = code == 0
    (root / "cmd-result.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    return code


if __name__ == "__main__":
    if sys.argv[1:2] == ["report"]:
        sys.exit(report())
    if sys.argv[1:2] == ["inside"]:
        sys.exit(inside(sys.argv[2:]))
    sys.exit(wrapped(sys.argv[1:]))
