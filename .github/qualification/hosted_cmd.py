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
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


OWNER = "//runsc/cmd:cmd_test"
FIXTURE = "//test/rbe/hosted_cmd:original"
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
             "/usr/bin/python3", "-IB", str(Path(__file__).resolve()), "inside", *args])


def inside(args):
    if os.getuid() != 0 or any(namespace(k) == os.environ.pop("PARENT_NS_" + k)
                              for k in NAMESPACES):
        return 3
    print("HOSTED_STAGE private namespaces", flush=True)
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
        print(PROOF + " " + os.environ["TEST_HOSTED_ID"], flush=True)
        return subprocess.run(args, check=False).returncode


def make_capsule(output):
    # rules_go exposes the exact test executable below its configured directory.
    print("HOSTED_STAGE locating owner", flush=True)
    candidates = [p for p in Path("bazel-bin/runsc/cmd").glob("**/cmd_test")
                  if p.is_file() and Path(str(p) + ".runfiles/_main").is_dir()]
    if len(candidates) != 1:
        raise ValueError("ambiguous executable")
    binary = candidates[0]
    destination = Path(output).resolve()
    dest_tar = destination / "cmd.tar"
    print("HOSTED_STAGE archiving runfiles", flush=True)
    subprocess.run(["/bin/tar", "--hard-dereference", "-chf", str(dest_tar),
                    "cmd_test", "cmd_test.runfiles"], check=True, cwd=binary.parent)
    if not 0 < dest_tar.stat().st_size < 1024 ** 3:
        raise ValueError("unexpected capsule size")
    identity = {"product": "353c484b45fec1ba5ba19445ba03807a5f9638e0",
                "owner": OWNER, "elf": digest(binary), "tar": digest(dest_tar)}
    (destination / "identity.json").write_text(json.dumps(identity))


def capsule(archive, expected):
    print("HOSTED_STAGE checking capsule", flush=True)
    identity = json.loads(Path(expected).read_text())
    if (identity["product"] != "353c484b45fec1ba5ba19445ba03807a5f9638e0" or
            identity["owner"] != OWNER or digest(Path(archive)) != identity["tar"]):
        return 8
    stage = Path(os.environ["TEST_TMPDIR"]).resolve() / "cmd-package"
    stage.mkdir(mode=0o755)
    with tarfile.open(archive) as bundle:
        for entry in bundle:
            name = Path(entry.name)
            if (".." in name.parts or name.is_absolute() or not name.parts
                    or name.parts[0] not in ("cmd_test", "cmd_test.runfiles")
                    or not (entry.isreg() or entry.isdir())):
                return 9
    subprocess.run(["/bin/tar", "--no-same-owner", "-xf", str(Path(archive).resolve()),
                    "-C", str(stage)], check=True)
    print("HOSTED_STAGE extracted owner", flush=True)
    binary = stage / "cmd_test"
    if digest(binary) != identity["elf"]:
        return 10
    with binary.open("rb") as stream:
        hdr = stream.read(20)
    if hdr[:6] != b"\x7fELF\x02\x01" or int.from_bytes(hdr[18:20], "little") != 183:
        return 11
    runfiles = str((stage / "cmd_test.runfiles").resolve())
    for key in ("TEST_SRCDIR", "RUNFILES_DIR", "JAVA_RUNFILES", "PYTHON_RUNFILES"):
        os.environ[key] = runfiles
    for key in ("RUNFILES_MANIFEST_FILE", "RUNFILES_MANIFEST_ONLY"):
        os.environ.pop(key, None)
    os.environ["TEST_WORKSPACE"] = "_main"
    os.environ["TEST_TARGET"] = OWNER
    os.chdir(Path(runfiles) / "_main")
    return wrapped([str(binary.resolve())])


def disposition(text):
    reasons = {
        "insufficient privilege": ("operation not permitted", "permission denied"),
        "unmapped user": ("UID 65534 is not mapped",),
        "unreachable Sentry": ("cannot be executed by an unprivileged user",),
        "missing file": ("no such file or directory",),
        "posture comparison": ("threads.uniform", "security posture mismatch"),
        "missing kernel feature": ("this kernel cannot report",),
    }
    reasons.update({k: (k,) for k in ("ambiguous executable", "unexpected capsule size", "no space left on device", "file size limit exceeded")})
    return sorted(k for k, terms in reasons.items() if any(t.lower() in text.lower() for t in terms))


def report():
    root = Path(os.environ["RUNNER_TEMP"])
    release = platform.release()
    result = {"outerBazelOwner": FIXTURE, "unchangedGoOwner": OWNER, "nativeArm": platform.machine() == "aarch64",
              "kernel": release if KERNEL.fullmatch(release) else "unknown",
              "fiveSentryLeaves": {}, "fiveGoferLeaves": {}}
    for phase in ("build", "capsule", "local"):
        status = root / ("cmd-" + phase + ".exit")
        result[phase + "Exit"] = int(status.read_text()) if status.exists() else None
        log = root / ("cmd-" + phase + ".log")
        if log.exists():
            result[phase + "Signals"] = disposition(log.read_text(errors="replace"))
    bep = root / "cmd-local.bep"
    records = []
    failures = set()
    if bep.exists():
        with bep.open() as events:
            for line in events:
                event = json.loads(line)
                ident = event.get("id", {}).get("testResult", {})
                detail = event.get("finished", {}).get("failureDetail", {})
                for domain, value in detail.items():
                    if isinstance(value, dict):
                        code = value.get("code", "")
                        if re.fullmatch(r"[A-Z][A-Z0-9_]{1,80}", code):
                            failures.add(code)
                if ident.get("label") == FIXTURE:
                    records.append(event["testResult"])
    result["localBazelFailureCodes"] = sorted(failures)
    result["testResults"] = len(records)
    strategy = records[0].get("executionInfo", {}).get("strategy", "") if len(records)==1 else ""
    result["strategy"] = strategy if strategy in ("local", "standalone") else "unreported"
    base = Path("bazel-testlogs/test/rbe/hosted_cmd/original")
    code = 1
    log = base / "test.log"
    if records and log.is_file():
        content = log.read_text(errors="replace")
        result["hostedLauncherObserved"] = (PROOF + " " + os.environ["GITHUB_RUN_ID"]) in content
        result["freshStandalone"] = (len(records) == 1 and not records[0].get("cachedLocally", False)
                                     and not records[0].get("executionInfo", {}).get("cachedRemotely", False)
                                     and result["hostedLauncherObserved"])
        result["fixtureSteps"] = [k for k in ("checking capsule", "extracted owner", "private namespaces") if ("HOSTED_STAGE " + k) in content]
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
                and result["localExit"] == result["buildExit"] == result["capsuleExit"] == 0
                and result["fiveSentryLeaves"] == dict.fromkeys(MODES, "PASS")):
            code = 0
    identity = root / "hosted-fixture/identity.json"
    if identity.exists():
        values = json.loads(identity.read_text())
        for k in ("elf", "tar"):
            if re.fullmatch("[0-9a-f]{64}", values.get(k, "")):
                result[k + "SHA256"] = values[k]
    result["passed"] = code == 0
    (root / "cmd-result.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    return code


if __name__ == "__main__":
    if sys.argv[1:2] == ["make"]:
        make_capsule(sys.argv[2])
        sys.exit(0)
    if sys.argv[1:2] == ["capsule"]:
        sys.exit(capsule(*sys.argv[2:]))
    if sys.argv[1:2] == ["report"]:
        sys.exit(report())
    if sys.argv[1:2] == ["inside"]:
        sys.exit(inside(sys.argv[2:]))
    sys.exit(wrapped(sys.argv[1:]))
