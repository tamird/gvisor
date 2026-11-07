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
from urllib.parse import unquote, urlparse


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
PRODUCT = "4eb27c4081e535d69c1257998a6c94256d9812e2"
STATUSES = {"PASSED", "FAILED", "FLAKY", "TIMEOUT", "INCOMPLETE", "REMOTE_FAILURE",
            "FAILED_TO_BUILD", "TOOL_HALTED_BEFORE_TESTING", "NO_STATUS"}


def namespace(kind):
    return os.readlink("/proc/self/ns/" + kind)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as data:
        for block in iter(lambda: data.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def shell_exit(code):
    return 128 - code if code < 0 else code


def record(path, value):
    # Each destination is a regular file created by the unprivileged fixture.
    with path.open("r+") as f:
        f.seek(0)
        json.dump(value, f)
        f.truncate()
        f.flush()
        os.fsync(f.fileno())


def wrapped(args):
    if os.getuid() == 0 or platform.machine() != "aarch64":
        return 2
    allowed = {"HOME", "LANG", "PATH", "CGROUPV2", "XML_OUTPUT_FILE", "GUNIT_OUTPUT",
               "JAVA_RUNFILES", "PYTHON_RUNFILES"}
    env = [f"{k}={v}" for k, v in os.environ.items()
           if k in allowed or k.startswith(("TEST_", "RUNFILES_", "GO_TEST_", "GTEST_"))]
    env.extend(f"PARENT_NS_{kind}={namespace(kind)}" for kind in NAMESPACES)
    finished = subprocess.run(["/usr/bin/sudo", "-n", "--", "/usr/bin/unshare", "--fork",
             "--mount", "--propagation", "private", "--mount-proc", "--pid", "--net",
             "--uts", "--ipc", "--", "/usr/bin/env", "-i", *env,
             "/usr/bin/python3", "-IB", str(Path(__file__).resolve()), "inside", *args], check=False)
    return shell_exit(finished.returncode)


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
        relay = Path(os.environ.pop("TEST_HOSTED_RELAY"))
        # The Bazel test action runs as the hosted runner. Keep files created
        # by the original root-only tests in a directory root can clean up.
        with tempfile.TemporaryDirectory(prefix="hosted-cmd-inner-", dir="/tmp") as scratch:
            work = Path(scratch)
            temporary = work / "tmp"
            temporary.mkdir()
            os.environ["TEST_TMPDIR"] = os.environ["TMPDIR"] = str(temporary)
            xml = work / "test.xml"
            os.environ["XML_OUTPUT_FILE"] = str(xml)
            for key in ("TEST_UNDECLARED_OUTPUTS_DIR", "TEST_UNDECLARED_OUTPUTS_ANNOTATIONS_DIR"):
                if key in os.environ:
                    directory = work / key
                    directory.mkdir()
                    os.environ[key] = str(directory)
            with tempfile.TemporaryFile(dir=work) as original:
                completed = subprocess.run(args, stdout=original, stderr=subprocess.STDOUT, check=False)
                # Record the original ELF's status before any copying, XML
                # parsing, temp directory cleanup, or return through unshare.
                record(relay / "inner-exit", completed.returncode)
                original.seek(0)
                markers = []
                streams = {"goWrapperError": False, "goXMLWriteError": False, "invalidFilterObserved": False}
                for line in original:
                    sys.stdout.buffer.write(line)
                    token = line.rstrip(b"\r\n").removeprefix(b"\x16")
                    if token in (b"PASS", b"FAIL"):
                        markers.append(token.decode())
                    if b"error while generating testreport" in line:
                        streams["goWrapperError"] = True
                    if b"error writing test xml" in line:
                        streams["goXMLWriteError"] = True
                    if b"invalid regexp" in line and b"-test.run" in line:
                        streams["invalidFilterObserved"] = True
                sys.stdout.buffer.flush()
            data = dict(streams, packageMarkers=markers,
                        innerXMLRootOwned=xml.is_file() and xml.stat().st_uid == 0,
                        innerTmpHasRootOwnedChildren=any(p.lstat().st_uid == 0 for p in temporary.iterdir()))
            record(relay / "inner-facts", data)
            if xml.is_file():
                # copyfile truncates the existing runner-owned inode. The root
                # process never creates or opens Bazel's declared output file.
                shutil.copyfile(xml, relay / "xml")
            return shell_exit(completed.returncode)


def built_owner(binary, bep):
    """Bind the materialized executable to the owner's default output group."""
    wanted = digest(binary)
    matched = set()
    children = {}
    roots = set()
    config = None
    with bep.open() as events:
        for line in events:
            event = json.loads(line)
            eid = event.get("id", {})
            if "namedSet" in eid and "namedSetOfFiles" in event:
                key = eid["namedSet"]["id"]
                content = event.get("namedSetOfFiles", {})
                children[key] = [s["id"] for s in content.get("fileSets", [])]
                for file in content.get("files", []):
                    if Path(file.get("name", "")).name != "cmd_test":
                        continue
                    uri = urlparse(file.get("uri", ""))
                    if uri.scheme == "file" and not uri.netloc:
                        bound = Path(unquote(uri.path)).resolve() == binary.resolve()
                        if file.get("digest") and file["digest"] != wanted:
                            bound = False
                    else:
                        bound = file.get("digest") == wanted or bool(
                            re.search(r"/(?:blobs|compressed-blobs/zstd)/" + wanted + r"/[0-9]+$", uri.path))
                    if bound:
                        matched.add(key)
            target = eid.get("targetCompleted", {})
            if target.get("label") in {OWNER, "@@" + OWNER} and not target.get("aspect"):
                payload = event.get("completed", {})
                if payload.get("success"):
                    config = target.get("configuration", {}).get("id")
                    for group in payload.get("outputGroup", []):
                        if group.get("name") == "default":
                            roots.update(s["id"] for s in group.get("fileSets", []))
    seen = set()
    while roots:
        key = roots.pop()
        if key not in seen:
            seen.add(key)
            roots.update(children.get(key, []))
    facts = {"namedSets": len(children), "ownerDefaultClosureSets": len(seen),
             "matchingOutputSets": len(matched), "matchingOwnerSets": len(seen.intersection(matched)),
             "ownerConfigurationObserved": bool(re.fullmatch(r"[a-f0-9]{8,128}", config or ""))}
    (bep.parent / "cmd-output-binding.json").write_text(json.dumps(facts))
    if not facts["matchingOwnerSets"] or not facts["ownerConfigurationObserved"]:
        raise ValueError("missing original owner output binding")
    return {"owner": OWNER, "configuration": config, "outputSHA256": wanted}


def make_capsule(output):
    # rules_go exposes the exact test executable below its configured directory.
    print("HOSTED_STAGE locating owner", flush=True)
    candidates = [p for p in Path("bazel-bin/runsc/cmd").glob("**/cmd_test")
                  if p.is_file() and Path(str(p) + ".runfiles/_main").is_dir()]
    if len(candidates) != 1:
        raise ValueError("ambiguous executable")
    binary = candidates[0]
    destination = Path(output).resolve()
    print("HOSTED_STAGE binding original owner", flush=True)
    producer = built_owner(binary, destination.parent / "cmd-build.bep")
    dest_tar = destination / "cmd.tar"
    print("HOSTED_STAGE archiving runfiles", flush=True)
    subprocess.run(["/bin/tar", "--hard-dereference", "-chf", str(dest_tar),
                    "cmd_test", "cmd_test.runfiles"], check=True, cwd=binary.parent)
    if not 0 < dest_tar.stat().st_size < 1024 ** 3:
        raise ValueError("unexpected capsule size")
    identity = {"product": PRODUCT,
                "owner": OWNER, "producer": producer, "elf": digest(binary), "tar": digest(dest_tar)}
    (destination / "identity.json").write_text(json.dumps(identity))


def capsule(archive, expected):
    # Bazel owns TEST_TMPDIR and removes it after the test. Keep the immutable
    # compiler output tree under the fixture's own disposable directory.
    cwd = Path.cwd()
    parent = Path(os.environ["TEST_HOSTED_RECORD_ROOT"]).resolve().parent
    with tempfile.TemporaryDirectory(prefix="cmd-package-", dir=parent) as folder:
        try:
            return run_capsule(archive, expected, Path(folder))
        finally:
            os.chdir(cwd)


def run_capsule(archive, expected, stage):
    print("HOSTED_STAGE checking capsule", flush=True)
    identity = json.loads(Path(expected).read_text())
    if (identity["product"] != PRODUCT or
            identity["owner"] != OWNER or digest(Path(archive)) != identity["tar"]):
        return 8
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
    if digest(binary) != identity["elf"] or identity["producer"]["outputSHA256"] != identity["elf"]:
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
    relay_root = Path(os.environ["TEST_HOSTED_RECORD_ROOT"]).resolve()
    xml = Path(os.environ["XML_OUTPUT_FILE"]).resolve()
    launcher = [sys.executable, "-IB", str(Path(__file__).resolve()), "once"]
    os.chdir(Path(runfiles) / "_main")
    with tempfile.NamedTemporaryFile(prefix="pair-", dir=relay_root, delete=False) as pair:
        pair_path = Path(pair.name)
    os.environ["TEST_HOSTED_PAIR"] = pair_path.name
    read_only_dirs = any(not os.access(p, os.W_OK) for p, _, _ in os.walk(stage))
    with tempfile.TemporaryDirectory(prefix="cmd-exit-control-", dir=relay_root) as control:
        negative = subprocess.run([*launcher, "invalid-filter", str(binary.resolve()),
                                   str(Path(control) / "test.xml")], check=False)
    pair_result = {"invalidFilterProcessExit": shell_exit(negative.returncode),
                   "readOnlyArchiveDirectory": read_only_dirs}
    record(pair_path, pair_result)
    if negative.returncode == 0:
        return 13
    complete = subprocess.run([*launcher, "complete-owner", str(binary.resolve()), str(xml)], check=False)
    pair_result["completeOwnerProcessExit"] = shell_exit(complete.returncode)
    record(pair_path, pair_result)
    return pair_result["completeOwnerProcessExit"]


def one_execution(purpose, binary, xml):
    relay_root = Path(os.environ.pop("TEST_HOSTED_RECORD_ROOT")).resolve()
    relay = Path(tempfile.mkdtemp(prefix="attempt-", dir=relay_root))
    (relay / "purpose").write_text(purpose)
    (relay / "pair").write_text(os.environ.pop("TEST_HOSTED_PAIR"))
    for name in ("inner-exit", "inner-facts", "xml"):
        (relay / name).touch(mode=0o600, exist_ok=False)
    os.environ["TEST_HOSTED_RELAY"] = str(relay)
    flags = ["-test.run=["] if purpose == "invalid-filter" else []
    outer = wrapped([binary, *flags])
    original = (relay / "inner-exit").read_text()
    status = shell_exit(int(original)) if original else None
    forwarded = status if status else outer
    try:
        if (relay / "xml").stat().st_size:
            shutil.copyfile(relay / "xml", xml)
        outputs_owned = (all((relay / name).stat().st_uid == os.getuid() for name in
                             ("inner-exit", "inner-facts", "xml")) and xml.is_file()
                         and xml.stat().st_uid == os.getuid())
    except OSError:
        outputs_owned = False
    if forwarded == 0 and not outputs_owned:
        forwarded = 12
    (relay / "outer").write_text(json.dumps(
        {"namespaceExit": outer, "returnedExit": forwarded, "runnerOwnsOutputs": outputs_owned}))
    print("HOSTED_RETURN " + relay.name + " " + str(forwarded), flush=True)
    return forwarded


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


def attempt_outputs(root):
    records = {}
    store = root / "hosted-fixture/records"
    if not store.is_dir():
        return records
    for path in sorted(store.glob("attempt-*"), key=lambda p: p.stat().st_mtime_ns):
        item = {"ordinal": len(records) + 1}
        purpose = (path / "purpose").read_text()
        item["purpose"] = purpose if purpose in ("complete-owner", "invalid-filter") else "unknown"
        pair = (path / "pair").read_text()
        if re.fullmatch(r"pair-[A-Za-z0-9_-]+", pair) and (store / pair).stat().st_size:
            pair_facts = json.loads((store / pair).read_text())
            if isinstance(pair_facts.get("readOnlyArchiveDirectory"), bool):
                item["readOnlyArchiveDirectory"] = pair_facts["readOnlyArchiveDirectory"]
            actual = pair_facts.get("completeOwnerProcessExit" if purpose == "complete-owner" else "invalidFilterProcessExit")
            if isinstance(actual, int) and 0 <= actual <= 255:
                item["launcherProcessExit"] = actual
        for name in ("inner-exit", "inner-facts", "outer"):
            target = path / name
            if not target.exists() or not 0 < target.stat().st_size <= 8192:
                continue
            raw = json.loads(target.read_text())
            if name == "inner-exit":
                if isinstance(raw, int) and -128 <= raw <= 255:
                    item["originalELFExit"] = raw
            elif isinstance(raw, dict):
                for k in ("goWrapperError", "goXMLWriteError", "innerXMLRootOwned",
                          "innerTmpHasRootOwnedChildren", "runnerOwnsOutputs", "invalidFilterObserved"):
                    if isinstance(raw.get(k), bool):
                        item[k] = raw[k]
                for k in ("namespaceExit", "returnedExit"):
                    if isinstance(raw.get(k), int) and 0 <= raw[k] <= 255:
                        item[k] = raw[k]
                if "packageMarkers" in raw and all(t in ("PASS", "FAIL") for t in raw["packageMarkers"]):
                    item["packageMarkers"] = raw["packageMarkers"][:64]
        records[path.name] = item
    return records


def result_record(event, ident, attempts, workspace):
    payload = event.get("testResult", {})
    info = payload.get("executionInfo", {})
    item = {k: ident.get(k, 0) for k in ("run", "shard", "attempt")}
    config = ident.get("configuration", {}).get("id", "")
    item["configuration"] = config if re.fullmatch(r"[0-9a-f]{8,128}", config) else None
    item["status"] = payload.get("status") if payload.get("status") in STATUSES else "unreported"
    item["strategy"] = info.get("strategy") if info.get("strategy") in ("local", "standalone", "sandboxed") else "unreported"
    item["cached"] = payload.get("cachedLocally", False) or info.get("cachedRemotely", False)
    item["actionExit"] = int(info.get("exitCode", 0)) if info else None
    item["actionExitExplicit"] = "exitCode" in info
    item["details"] = disposition(" ".join([payload.get("statusDetails", ""), *payload.get("warning", [])]))
    for f in payload.get("testActionOutput", []):
        if f.get("name") not in ("test.log", "test.xml"):
            continue
        uri = urlparse(f.get("uri", ""))
        if uri.scheme != "file" or uri.netloc:
            continue
        path = Path(unquote(uri.path)).resolve()
        if not path.is_relative_to(workspace) or not path.is_file():
            continue
        item[f["name"] + "SHA256"] = digest(path)
        if f["name"] == "test.log":
            item["hostedAttempts"] = [attempts[m[1]]["ordinal"] for m in re.finditer(
                r"(?m)^HOSTED_RETURN (attempt-[A-Za-z0-9_-]+) ([0-9]{1,3})$", path.read_text(errors="replace"))
                if m[1] in attempts]
    return item


def test_vocabulary():
    """Only publish test names and locations that occur in the public source."""
    tests = set()
    literals = set()
    files = {}
    for path in Path("runsc/cmd").glob("*_test.go"):
        source = path.read_text()
        files[path.name] = len(source.splitlines())
        tests.update(re.findall(r"^func (Test\w+)\(t \*testing.T\)", source, re.M))
        for literal in re.findall(r'"([^"\\\n]{1,200})"', source):
            for part in literal.split("/"):
                literals.add(re.sub(r"\s", "_", part))
    return tests, literals, files


def case_id(name, tests, literals):
    root, *parts = name.split("/")
    if root in tests and all(re.sub(r"#\d{2,}$", "", p) in literals for p in parts):
        return name
    # Preserve every outcome even for an anonymous or runtime-generated case.
    return (root if root in tests else "case") + "/sha256-" + hashlib.sha256(name.encode()).hexdigest()


def diagnostic(text, files):
    locations = sorted({f"{file}:{number}" for file, number in
                        re.findall(r"\b([\w]+_test\.go):(\d{1,6}):", text)
                        if file in files and 0 < int(number) <= files[file]})
    phrases = {
        "finding the sentry binary": "locate-sentry",
        "checking whether": "check-executable-path",
        "cannot be executed by an unprivileged user": "executable-path",
        "error setting up container": "setup-container",
        "error creating container": "create-container",
        "error describing the reference process": "describe-reference",
        "error starting container": "start-container",
        "error collecting posture": "collect-posture",
        "operation not permitted": "EPERM", "permission denied": "EACCES",
        "no such file or directory": "ENOENT", "resource temporarily unavailable": "EAGAIN",
        "no space left on device": "ENOSPC", "context deadline exceeded": "deadline",
        "signal: killed": "killed", "connection reset by peer": "ECONNRESET",
    }
    lower = text.lower()
    return {"locations": locations[:12], "categories": [code for term, code in phrases.items() if term in lower]}


def report():
    root = Path(os.environ["RUNNER_TEMP"])
    release = platform.release()
    result = {"source": PRODUCT, "outerBazelOwner": FIXTURE, "unchangedGoOwner": OWNER, "nativeArm": platform.machine() == "aarch64",
              "kernel": release if KERNEL.fullmatch(release) else "unknown",
              "fiveSentryLeaves": {}, "fiveGoferLeaves": {}}
    for phase in ("build", "capsule", "local"):
        status = root / ("cmd-" + phase + ".exit")
        result[phase + "Exit"] = int(status.read_text()) if status.exists() else None
        log = root / ("cmd-" + phase + ".log")
        if log.exists():
            text = log.read_text(errors="replace")
            result[phase + "Signals"] = disposition(text)
            if phase == "capsule":
                result["capsuleStages"] = [s for s in ("locating owner", "binding original owner", "archiving runfiles")
                                           if "HOSTED_STAGE " + s in text]
                result["capsuleErrors"] = [e for e in ("ambiguous executable", "missing original owner output binding",
                                                       "unexpected capsule size", "File too large", "No space left on device",
                                                       "CalledProcessError", "PermissionError") if e in text]
    binding = root / "cmd-output-binding.json"
    if binding.is_file():
        result["outputBinding"] = json.loads(binding.read_text())
    bep = root / "cmd-local.bep"
    records = []
    failures = set()
    attempts = attempt_outputs(root)
    result["originalExecutions"] = list(attempts.values())
    workspace = Path("bazel-testlogs/test/rbe/hosted_cmd").resolve()
    if bep.exists():
        with bep.open() as events:
            for line in events:
                event = json.loads(line)
                ident = event.get("id", {}).get("testResult", {})
                for source in ("finished", "action", "completed"):
                    detail = event.get(source, {}).get("failureDetail", {})
                    for value in detail.values():
                        if isinstance(value, dict):
                            code = value.get("code", "")
                            if re.fullmatch(r"[A-Z][A-Z0-9_]{1,80}", code):
                                failures.add(code)
                if ident.get("label") == FIXTURE:
                    records.append(result_record(event, ident, attempts, workspace))
    result["localBazelFailureCodes"] = sorted(failures)
    result["testResults"] = len(records)
    result["bazelAttempts"] = records
    base = Path("bazel-testlogs/test/rbe/hosted_cmd/original")
    code = 1
    log = base / "test.log"
    if records and log.is_file():
        content = log.read_text(errors="replace")
        result["hostedLauncherObserved"] = (PROOF + " " + os.environ["GITHUB_RUN_ID"]) in content
        result["freshStandalone"] = (result["hostedLauncherObserved"] and bool(attempts)
                                     and all(not r["cached"] for r in records))
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
            tests, literals, files = test_vocabulary()
            by_case = {}
            current = None
            for line in content.splitlines():
                started = re.match(r"^\s*=== (?:RUN|CONT|NAME)\s+(\S+)", line)
                if started:
                    current = started[1]
                elif re.match(r"^\s*--- (?:PASS|FAIL|SKIP): ", line):
                    current = None
                elif current and ("_test.go:" in line or "error" in line.lower()):
                    by_case.setdefault(current, []).append(line)
            result["cases"] = []
            counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
            for case in ET.parse(xml).iter("testcase"):
                name = case.get("name", "").removeprefix("cmd.")
                verdict = "FAIL" if any(case.find(n) is not None for n in ("failure", "error")) else (
                    "SKIP" if case.find("skipped") is not None else "PASS")
                counts[verdict] += 1
                row = {"name": case_id(name, tests, literals), "status": verdict}
                if verdict == "FAIL":
                    detail = " ".join([*(v.get("message", "") for v in case),
                                       *case.itertext(), *by_case.get(name, [])])
                    row.update(diagnostic(detail, files))
                result["cases"].append(row)
                if re.fullmatch(r"TestBootPosture/[A-Za-z_]+/(?:sentry|gofer)", name):
                    _, mode, process = name.split("/")
                    if mode in MODES:
                        leaves = result[{"sentry": "fiveSentryLeaves", "gofer": "fiveGoferLeaves"}[process]]
                        if leaves.get(mode, verdict) != verdict:
                            verdict = "CONFLICT"
                        leaves[mode] = verdict
            result["xmlCaseTotals"] = counts
        complete = [a for a in attempts.values() if a["purpose"] == "complete-owner"]
        controls = [a for a in attempts.values() if a["purpose"] == "invalid-filter"]
        if (result["freshStandalone"] and result["hostedLauncherObserved"] and result["nativeArm"]
                and result["localExit"] == result["buildExit"] == result["capsuleExit"] == 0
                and complete and controls
                and all(a.get("originalELFExit") == a.get("namespaceExit") == a.get("returnedExit")
                        == a.get("launcherProcessExit") and a.get("launcherProcessExit", 0) > 0
                        and a.get("invalidFilterObserved") for a in controls)
                and all(a.get("originalELFExit") == a.get("namespaceExit") == a.get("returnedExit")
                        == a.get("launcherProcessExit") == 0
                        and a.get("runnerOwnsOutputs") and "PASS" in a.get("packageMarkers", [])
                        and "FAIL" not in a["packageMarkers"] for a in complete)
                and all(r["status"] == "PASSED" for r in records)
                and len(records) == len(complete) == 1
                and records[0].get("test.xmlSHA256") == result.get("ownerXMLSHA256")
                and records[0].get("test.logSHA256") == result.get("ownerLogSHA256")
                and result.get("xmlCaseTotals", {}).get("FAIL") == 0
                and result["fiveSentryLeaves"] == result["fiveGoferLeaves"] == dict.fromkeys(MODES, "PASS")):
            code = 0
    identity = root / "hosted-fixture/identity.json"
    if identity.exists():
        values = json.loads(identity.read_text())
        result["producer"] = values["producer"]
        for k in ("elf", "tar"):
            if re.fullmatch("[0-9a-f]{64}", values.get(k, "")):
                result[k + "SHA256"] = values[k]
    result["passed"] = code == 0
    (root / "cmd-result.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    return code


if __name__ == "__main__":
    if sys.argv[1:2] == ["once"] and sys.argv[2] in ("complete-owner", "invalid-filter"):
        sys.exit(one_execution(sys.argv[2], sys.argv[3], Path(sys.argv[4])))
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
