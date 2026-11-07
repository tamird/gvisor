#!/usr/bin/env python3
"""Run the real cmd_test target and its declared guest on the native hosted kernel."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

OWNER = "//runsc/cmd:cmd_test"
SOURCE = "63def2617c897f22d884e4d2689498f3d7c7df82"
BASE = "3847ed738e1a59892b936aebb1b9164c4f31ee95"
NAMESPACES = ("mnt", "pid", "net", "uts", "ipc")
SIDECARS = {"checkpointgofer", "gvisor-proc-umounter", "gvisor-sentry-prewarmer", "gvisor_sentry",
            "runsc-fd-parking", "runsc-metric-server", "runsc-netgofer"}
MACHINES = {"aarch64": 183, "x86_64": 62}
MODES = ("default", "hostinet", "no_directfs", "no_directfs_hostinet", "fdlimit")
REQUIRED = {f"TestBootPosture/{mode}/{role}" for mode in MODES for role in ("sentry", "gofer")} | {
    "TestCapabilities/directfs", "TestCapabilities/lisafs", "TestStatefile/graph", "TestStatefile/wire",
    "TestCheckpointStateEngineFlag/override_default_graph_with_wire",
    "TestCheckpointStateEngineFlag/override_boot_wire_and_preserve_boot_default",
    "TestCheckpointStateEngineAnnotationsAndSubcommand/override_annotated_boot_graph_with_wire",
    "TestCheckpointStateEngineAnnotationsAndSubcommand/override_annotated_boot_wire_with_graph",
    "TestRootfsUpperExecute",
}
FAMILIES = sorted({case.split("/")[0] for case in REQUIRED})


def digest(p):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def elf(p, static=False):
    with p.open("rb") as f:
        h = f.read(64)
        assert h[:6] == b"\x7fELF\x02\x01"
        machine = int.from_bytes(h[18:20], "little")
        assert machine == MACHINES[platform.machine()]
        f.seek(int.from_bytes(h[32:40], "little"))
        size = int.from_bytes(h[54:56], "little")
        count = int.from_bytes(h[56:58], "little")
        assert 56 <= size <= 128 and 0 < count < 200
        headers = [f.read(size) for _ in range(count)]
        assert all(len(header) == size for header in headers)
        kinds = [int.from_bytes(header[:4], "little") for header in headers]
        assert 1 in kinds
        needed = 0
        for header, kind in zip(headers, kinds):
            if kind == 2:
                f.seek(int.from_bytes(header[8:16], "little"))
                length = int.from_bytes(header[32:40], "little")
                assert length % 16 == 0 and length < 1024 * 1024
                for _ in range(length // 16):
                    tag = int.from_bytes(f.read(16)[:8], "little")
                    if tag == 0:
                        break
                    needed += tag == 1
        if static:
            assert 3 not in kinds and not needed, "guest loader or dynamic dependencies"
    return {"machine": machine, "size": p.stat().st_size, "sha256": digest(p),
            "interpreter": 3 in kinds, "needed": needed}


def ns(k):
    return os.readlink("/proc/self/ns/" + k)


def run_under(args):
    # Bazel sets this from the go_test's $(rootpath); never inject a guessed value.
    app = os.environ["GVISOR_CMD_TEST_APP"]
    root = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"]
    assert not Path(app).is_absolute() and ".." not in Path(app).parts and app.startswith("test/cmd/test_app/")
    assert os.environ["TEST_TARGET"] == OWNER and os.getuid() != 0
    source = root / app
    assert source.is_file() and os.access(source, os.X_OK)
    actual = elf(source, static=True)
    allow = {"HOME", "LANG", "PATH", "CGROUPV2", "GVISOR_CMD_TEST_APP", "XML_OUTPUT_FILE",
             "JAVA_RUNFILES", "PYTHON_RUNFILES"}
    env = [f"{k}={v}" for k, v in os.environ.items()
           if k in allow or k.startswith(("TEST_", "RUNFILES_", "GO_TEST_", "GTEST_"))]
    env.extend(f"PARENT_NS_{k}={ns(k)}" for k in NAMESPACES)
    with tempfile.TemporaryDirectory(prefix="hosted-cmd-relay-", dir="/tmp") as tmp:
        relay = Path(tmp)
        for name in ("xml", "facts.json"):
            (relay / name).touch()
        code = subprocess.run(["/usr/bin/sudo", "-n", "--", "/usr/bin/unshare", "--fork",
                               "--mount", "--propagation", "private", "--mount-proc", "--pid",
                               "--net", "--uts", "--ipc", "--", "/usr/bin/env", "-i", *env,
                               "/usr/bin/python3", "-IB", str(Path(__file__).resolve()), "inside",
                               str(relay), *args]).returncode
        if (relay / "xml").stat().st_size:
            shutil.copyfile(relay / "xml", os.environ["XML_OUTPUT_FILE"])
        inner = json.loads((relay / "facts.json").read_text())
        print("CMD_GUEST_ORIGINAL " + json.dumps({"rootpath": app, "test_app": actual, **inner},
                                                  sort_keys=True), flush=True)
        return code


def inside(relay, args):
    assert os.getuid() == 0 and all(ns(k) != os.environ.pop("PARENT_NS_" + k) for k in NAMESPACES)
    src = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"] / "release/gvisor-bin"
    assert {p.name for p in src.iterdir()} == SIDECARS
    with (tempfile.TemporaryDirectory(prefix="hosted-cmd-sidecars-", dir="/tmp") as raw,
          tempfile.TemporaryDirectory(prefix="hosted-cmd-inner-", dir="/tmp") as private,
          tempfile.TemporaryDirectory(prefix="hosted-cmd-chroot-", dir="/tmp") as mountpoint):
        target = Path(raw)
        target.chmod(0o755)
        for name in SIDECARS:
            f = target / name
            shutil.copyfile(src / name, f)
            f.chmod(0o755)
            assert digest(f) == digest(src / name)
        Path(mountpoint).chmod(0o711)
        work = Path(private)
        (work / "tmp").mkdir()
        xml = work / "test.xml"
        os.environ.update(GVISOR_SIDECAR_BINARIES_DIR=raw, TMPDIR=mountpoint,
                          TEST_TMPDIR=str(work / "tmp"), XML_OUTPUT_FILE=str(xml))
        for k in ("TEST_UNDECLARED_OUTPUTS_DIR", "TEST_UNDECLARED_OUTPUTS_ANNOTATIONS_DIR"):
            (work / k).mkdir()
            os.environ[k] = str(work / k)
        result = subprocess.run(args).returncode
        if xml.is_file():
            shutil.copyfile(xml, Path(relay) / "xml")
        (Path(relay) / "facts.json").write_text(json.dumps(
            {"kernel": platform.release(), "uname": platform.machine(), "sidecars": len(SIDECARS),
             "private_namespaces": list(NAMESPACES), "inner_exit": result}))
        return result



def shell(*args):
    return subprocess.check_output(args, text=True).strip()


def collect(bep, dest, arm):
    records = []
    for line in bep.read_text().splitlines():
        event = json.loads(line)
        key = event.get("id", {}).get("testResult")
        if not key or key.get("label") != OWNER or "testResult" not in event:
            continue
        result = event["testResult"]
        files = {}
        for entry in result.get("testActionOutput", []):
            if entry["name"] in ("test.log", "test.xml") and entry.get("uri", "").startswith("file:"):
                files[entry["name"]] = Path(unquote(urlparse(entry["uri"]).path))
        row = {"status": result["status"], "cached": result.get("cachedLocally", False),
               "strategy": result.get("executionInfo", {}).get("strategy", "")}
        records.append(row)
        if set(files) != {"test.xml", "test.log"}:
            row["outputs"] = list(files)
            continue
        log = files["test.log"].read_text(errors="replace")
        row["original_digests"] = {k: digest(p) for k, p in files.items()}
        row["observations"] = [json.loads(s.partition("CMD_GUEST_ORIGINAL ")[2])
                               for s in log.splitlines() if s.startswith("CMD_GUEST_ORIGINAL ")]
        cases, verbose, skips, seconds = {}, {}, {}, {}
        for case in ET.parse(files["test.xml"]).iter("testcase"):
            name = case.attrib["name"].removeprefix("cmd.")
            if name.split("/")[0] not in FAMILIES:
                continue
            assert name not in cases
            status = ("FAIL" if case.find("failure") is not None or case.find("error") is not None
                      else "SKIP" if case.find("skipped") is not None else "PASS")
            cases[name] = status
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", case.get("time", "")):
                seconds[name] = float(case.attrib["time"])
            verbose[name] = bool(re.search(r"--- " + status + ": " + re.escape(name) + r"(?:\s|$)", log))
            if status == "SKIP":
                skips[name] = True
        row.update(cases=cases, verbose=verbose, skips=sorted(skips), xml_seconds=seconds)
        row["missing_sleep"] = any("/bin/sleep" in line and any(e in line.lower() for e in
                                        ("no such file or directory", "file does not exist", "not found"))
                                   for line in log.splitlines())
        row["diagnostics"] = [s for s in ("permission denied", "connection reset by peer",
                              "GVISOR_CMD_TEST_APP is not set", "copy test_app to guest root",
                              "operation not permitted", "directory __main__ not found",
                              "exec format error", "no such file or directory", "Traceback (most recent call last)")
                              if s in log]
    dest["test_results"] = records
    if len(records) != 1:
        return False
    r = records[0]
    if r["cached"] or r["strategy"] not in {"standalone", "local"} or len(r.get("observations", [])) != 1:
        return False
    cases = r.get("cases", {})
    if arm == "positive":
        return r["status"] == "PASSED" and all(cases.get(n) == "PASS" and r["verbose"][n] for n in REQUIRED) and all(
            cases.get("TestBootPosture/" + n) in {"PASS", "SKIP"} for n in ("kvm", "coretags"))
    return (r["status"] == "FAILED" and cases.get("TestRootfsUpperExecute") == "FAIL" and
            r["verbose"]["TestRootfsUpperExecute"] and r["missing_sleep"])


def qualify():
    work = Path(os.environ["RUNNER_TEMP"]) / "cmd-guest-fixture"
    cpu = platform.machine()
    assert shell("git", "rev-parse", "HEAD") == SOURCE
    assert shell("git", "rev-parse", "HEAD^") == BASE
    assert cpu in MACHINES and platform.release().startswith("6.8.")
    bazel = [str(work / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3000m"]
    flags = ["--config=" + cpu, "-c", "opt", "--strip=never", "--jobs=3", "--local_resources=memory=9900",
             "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
             "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
             "--remote_executor=", "--remote_cache=", "--bes_backend=", "--spawn_strategy=linux-sandbox",
             "--color=no", "--curses=no", "--noshow_progress"]
    report = {"source": SOURCE, "parent": BASE, "kernel": platform.release(), "owner": OWNER, "machine": cpu,
              "verified": False, "arms": {}}
    receipt = work / "result.json"
    receipt.write_text(json.dumps(report))
    path = Path("runsc/cmd/tar_test.go")
    tested = path.read_text()
    parent = shell("git", "show", BASE + ":" + str(path)) + "\n"
    original = 'spec := testutil.NewSpecWithArgs("/bin/sleep", "10000")'
    assert parent.count(original) == 1 and parent.replace(original, 'spec := idleGuestSpec(t)') == tested
    for arm in ("positive", "parent-command"):
        row = report["arms"][arm] = {}
        try:
            if arm == "parent-command":
                # Preserve the parent's original test and argv, changing only
                # its root to the candidate's same test_app-only directory.
                path.write_text(parent.replace(original, original + "\n\tspec.Root.Path = idleGuestSpec(t).Root.Path"))
            bep = work / (arm + ".bep")
            test = "^(" + "|".join(FAMILIES) + ")$" if arm == "positive" else "^TestRootfsUpperExecute$"
            args = [*bazel, "test", *flags, "--strategy=TestRunner=standalone",
                    "--run_under=/usr/bin/python3 -IB " + str(Path(__file__).resolve()) + " under",
                    "--nocache_test_results", "--flaky_test_attempts=1", "--test_timeout=900",
                    "--test_env=GO_TEST_WRAP_TESTV=1", "--test_env=CGROUPV2=true", "--test_output=errors",
                    "--build_event_json_file=" + str(bep), "--test_filter=" + test, OWNER]
            with (work / (arm + ".log")).open("w") as out:
                code = subprocess.run(args, stdout=out, stderr=subprocess.STDOUT).returncode
            row["exit"] = code
            row["verified"] = collect(bep, row, arm) and code == (0 if arm == "positive" else 3)
            if not row["verified"]:
                lines = (work / (arm + ".log")).read_text(errors="replace").splitlines()
                print(arm, "Bazel exit", code, flush=True)
                for line in [x for x in lines if x.startswith("ERROR:")][:8]:
                    print(line[:450], flush=True)
        except (KeyError, OSError, ValueError, AssertionError) as e:
            row["verified"] = False
            row["observer_error"] = type(e).__name__
            print(arm, repr(e)[:300], flush=True)
        finally:
            path.write_text(tested)
            receipt.write_text(json.dumps(report, indent=2))
    if all(r.get("verified") for r in report["arms"].values()):
        pos, neg = (r["test_results"][0]["observations"][0] for r in report["arms"].values())
        report["verified"] = pos["test_app"] == neg["test_app"] and pos["rootpath"] == neg["rootpath"]
    receipt.write_text(json.dumps(report, indent=2))
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    if sys.argv[1] == "inside":
        sys.exit(inside(sys.argv[2], sys.argv[3:]))
    if sys.argv[1] == "under":
        sys.exit(run_under(sys.argv[2:]))
    sys.exit(qualify())
