"""Read the original native consumer results for the static test_app runfile."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import traceback
from urllib.parse import unquote, urlsplit
import uuid
from xml.etree import ElementTree as ET

D = Path(os.environ["RUNNER_TEMP"]) / "test-app-runfile"
OLD = "f35d725669ae45e7617cf66e7ceee2f0f6307490"
NEW = "90dd28479a2fbd8bbbf0c9bd79ad9e9f9abb5b4c"
APP = "//test/cmd/test_app:test_app"
PRCTL = "//test/syscalls:prctl_test_native"
BOOT = "//runsc/container:boot_test"
GTEST = "PrctlTest.OrphansReparentedToSubreaper"
GO = "TestMinimalBoot"
PKG = Path("test/cmd/test_app")
DISTDIR = "--distdir=" + str(D / "dist")
PRIOR_ELF = "995658ee367b4d62044197fc09ba1bf356a3242acc0ce82a4a13caf337280459"
COMMON = ["--config=x86_64", "--jobs=4", "--remote_executor=", "--remote_cache=", "--bes_backend=",
          DISTDIR, "--lockfile_mode=off", "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
          "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash,//test/rbe/protoc_check:static_python"]
TEST = ["--nocache_test_results", "--flaky_test_attempts=1", "--runs_per_test=1", "--test_sharding_strategy=disabled",
        "--strategy=TestRunner=linux-sandbox", "--sandbox_default_allow_network=false", "--sandbox_add_mount_pair=/tmp",
        "--test_timeout=300", "--test_output=errors"]
R = {"status": "INCOMPLETE", "kernel": platform.release(), "historical_benchmark": {
    "source": "ca01b6885", "carrier": "de608f7a2", "run": 41691699509, "artifact":13259036378,
    "sha256":"692e4283970e8cb932c9a7cfed628d99f80bec092b6929d130e700d42b3b6f98"},
    "prior_revised_consumers": {"run": 42056452015, "artifact": 13428734445, "source": NEW,
        "zip_sha256": "7b4b95ffe7e373eb5d8aed3a27cb385eaca00a785f1c07d14c02652ad602b85e",
        "static_elf_sha256": PRIOR_ELF}, "arms": {}, "errors": {}}


def save():
    (D / "out/verdict.json").write_text(json.dumps(R, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def invoke(row, name, argv, seconds):
    console = D / (name + ".log")
    with console.open("wb") as stream:
        p = subprocess.run(["timeout", "--kill-after=15", str(seconds), D / "bazel", "--nosystem_rc", "--nohome_rc",
                            "--host_jvm_args=-Xmx4g", *argv], stdout=stream, stderr=subprocess.STDOUT)
    row.update(native_exit=p.returncode, console_sha256=digest(console))
    save()
    text = console.read_text(errors="replace")
    row["bazel_errors"] = []
    for line in text.splitlines():
        if line.startswith("ERROR:") and len(row["bazel_errors"]) < 3:
            line = re.sub(r"(?:https?|grpcs?)://\S+", "<URL>", line)
            for path, token in ((str(Path.cwd()), "$WORKSPACE"), (str(D), "$TEMP"), (str(Path.home()), "$HOME")):
                line = line.replace(path, token)
            row["bazel_errors"].append(line[:350])
    save()
    return text


def named_output(events, label, row):
    filesets = {e["id"]["namedSet"]["id"]: e["namedSetOfFiles"] for e in events if "namedSetOfFiles" in e}
    completed, = [e["completed"] for e in events
                  if e["id"].get("targetCompleted", {}).get("label") == label
                  and not e["id"]["targetCompleted"].get("aspect")]
    assert completed["success"]
    pending = [s["id"] for group in completed["outputGroup"] if group["name"] == "default" for s in group["fileSets"]]
    seen, files = set(), []
    while pending:
        key = pending.pop()
        if key not in seen:
            seen.add(key)
            files.extend(filesets[key].get("files", []))
            pending.extend(s["id"] for s in filesets[key].get("fileSets", []))
    name = label.split(":")[1]
    row["default_basenames"] = sorted({Path(f["name"]).name for f in files})[:16]
    save()
    entry, = [f for f in files if Path(f["name"]).name == name]
    uri = urlsplit(entry["uri"])
    assert uri.scheme == "file" and not (uri.netloc or uri.query or uri.fragment)
    path = Path(unquote(uri.path))
    base = Path("bazel-out").resolve(strict=True)
    base.relative_to((Path.home() / ".cache/bazel").resolve(strict=True))
    relative = path.resolve(strict=True).relative_to(base)
    package = Path(label.split(":")[0][2:])
    assert relative.parts[1:2 + len(package.parts)] == ("bin", *package.parts)
    return path


def elf(path):
    actual = path.resolve(strict=True)
    root = Path("bazel-out").resolve(strict=True)
    relative = actual.relative_to(root)
    assert "/bin/test/cmd/test_app/" in str(relative) and actual.is_file() and os.access(actual, os.X_OK)
    data = actual.read_bytes()
    headers = subprocess.check_output(["readelf", "-lW", "-dW", str(actual)], text=True)
    assert data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[18:20], "little") == 62
    assert "INTERP" not in headers and "(NEEDED)" not in headers
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "output": str(relative),
            "machine": 62, "interpreter": False, "needed": False}


def exact_report(result, target, name):
    item, = [f for f in result["testActionOutput"] if f["name"] == name]
    uri = urlsplit(item["uri"])
    assert uri.scheme == "file" and not (uri.netloc or uri.query or uri.fragment)
    base = Path("bazel-testlogs").resolve(strict=True)
    base.relative_to((Path.home() / ".cache/bazel").resolve(strict=True))
    path = base / target[2:].replace(":", "/") / name
    assert path.resolve(strict=True) == path == Path(unquote(uri.path)).resolve(strict=True)
    assert path.is_file() and 0 < path.stat().st_size < 12 * 1024**2
    return path


def outcomes(row, result, target):
    log, xml = (exact_report(result, target, n) for n in ("test.log", "test.xml"))
    row.update(log_sha256=digest(log), xml_sha256=digest(xml))
    body = log.read_text(errors="replace").replace("\x16", "")
    xml_cases = {}
    gtest = target == PRCTL
    for case in ET.parse(xml).iter("testcase"):
        name = case.get("name", "")
        full = "/".join(filter(None, (case.get("classname"), name))) if gtest else name
        if gtest and name == GTEST:
            full = GTEST.replace(".", "/")
        assert len(full) < 150 and re.fullmatch(r"[A-Za-z0-9_./:-]+", full)
        assert full not in xml_cases
        xml_cases[full] = ("FAIL" if any(case.find(t) is not None for t in ("error","failure")) else
                           "SKIP" if case.find("skipped") is not None or case.get("status") == "notrun"
                           or case.get("result") == "suppressed" else "PASS")
    row["xml"] = xml_cases
    terms = ("Copy:", "is a directory", "execve_errno", "Permission denied", "Operation not permitted",
             "permission denied", "operation not permitted", "Error running as root", "re-executing self",
             "creating default loopback interface", "creating container", "failed to start", "connection reset by peer",
             "device or resource busy", "No such file or directory", "no such file or directory")
    row["diagnostics"] = [t for t in terms if t in body]
    if gtest:
        row["gtest"] = {s: bool(re.search(r"\[\s*"+s+r"\s*\]\s+"+re.escape(GTEST)+r"\b", body))
                        for s in ("RUN","OK","FAILED")}
        row["execve_errno"] = [int(n) for n in re.findall(r"execve_errno\s*\n\s*Which is: (\d+)", body)][:3]
    else:
        row["go"] = {n:s for s,n in re.findall(r"^\s*--- (PASS|FAIL|SKIP): (TestMinimalBoot(?:/\w+)?) \(", body, re.M)}
    save()


def phase(arm, label, source, row):
    fixed = source == NEW
    name = arm + "-" + label
    if label == "nogo":
        suite = APP + "_nogo"
        real = APP + ("_bin_nogo" if fixed else "_nogo")
        # One query binds the expanded named test and its directly analyzed Go target.
        query = "tests(%s) union labels(deps, tests(%s))" % (suite, suite)
        body = invoke(row, name, ["query", DISTDIR, "--keep_going", "--lockfile_mode=off", "--output=label", query], 240)
        row["labels"] = sorted(re.findall(r"(?m)^//test/cmd/test_app:[\w]+$", body))
        assert row["native_exit"] == 0 and row["labels"] == sorted([real, APP + ("_bin" if fixed else "")])
        return
    target = {"public": APP, "prctl": PRCTL, "boot": BOOT}[label]
    run = label == "public"
    bep = D / (name + ".bep")
    uid = str(uuid.uuid4())
    args = ["run" if run else "test", *COMMON, "--invocation_id="+uid, "--build_event_json_file="+str(bep)]
    if run:
        args.extend([target, "--", "echo", "declared-test-app"])
    elif label == "prctl":
        args.extend([*TEST, "--test_env=GTEST_FILTER="+GTEST, target])
    else:
        args.extend([*TEST, "--test_arg=--test_platforms=systrap", "--test_arg=-test.run=^TestMinimalBoot$/^systrap$",
                     "--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1", target])
    body = invoke(row, name, args, {"public": 1200, "prctl": 1200, "boot": 1350}[label])
    if run:
        row["echo_lines"] = body.splitlines().count("declared-test-app")
        save()
    events = [json.loads(line) for line in bep.open() if line.strip()]
    assert [e["started"]["uuid"] for e in events if "started" in e] == [uid]
    row["invocation"] = uid
    if run:
        exe = named_output(events, target, row)
        row["elf"] = elf(exe)
        row["output_name"] = exe.name
        assert row["native_exit"] == 0 and row["echo_lines"] == 1 and exe.name == "test_app"
        if fixed:
            assert exe.resolve(strict=True) == (Path("bazel-bin")/PKG/"test_app").resolve(strict=True)
            assert row["elf"]["sha256"] == PRIOR_ELF
        return
    key, result = next((e["id"]["testResult"],e["testResult"]) for e in events if "testResult" in e)
    assert sum("testResult" in e for e in events) == 1 and key["label"] == target
    info = result["executionInfo"]
    row["test"] = {"target": target, "config": key["configuration"]["id"], "status": result["status"],
                   "strategy": info.get("strategy"), "cached": bool(result.get("cachedLocally") or info.get("cachedRemotely"))}
    save()
    # The original tests and the files consumed by them are independent evidence.
    try:
        outcomes(row, result, target)
    except (AssertionError, ET.ParseError, KeyError, OSError, ValueError) as exc:
        row["report_error"] = type(exc).__name__
        save()
    exe = named_output(events, target, row)
    direct = Path(str(exe) + ".runfiles") / "_main" / PKG / "test_app"
    row["direct"] = "file" if direct.is_file() else "directory" if direct.is_dir() else "absent"
    save()
    row["runfile"] = elf(direct if fixed else direct / "test_app")
    save()
    assert row["direct"] == ("file" if fixed else "directory")
    assert row["test"]["strategy"] == "linux-sandbox" and not row["test"]["cached"]
    status = "PASS" if fixed else "FAIL"
    if label == "prctl":
        assert row["xml"] == {GTEST.replace(".", "/"):status}
        assert row["gtest"]["RUN"] and row["gtest"]["OK" if fixed else "FAILED"]
        if not fixed:
            assert set(row["execve_errno"]) == {13}
    else:
        expected = {GO: status, **({GO+"/systrap": status} if fixed else {})}
        assert row["go"] == row["xml"] == expected
        if not fixed:
            assert {"Copy:", "is a directory"} <= set(row["diagnostics"])
    assert row["native_exit"] == (0 if fixed else 3) and row["test"]["status"] == ("PASSED" if fixed else "FAILED")
    row["consumer_observed"] = True
    save()
    assert row["runfile"]["sha256"] == R["arms"][arm]["public"]["elf"]["sha256"]


save()
assert platform.machine() == "x86_64" and os.getuid() != 0
assert git("rev-parse", NEW+"^") == OLD and git("rev-parse", NEW+"^{tree}") == "73124c1ea0b75d398e63a7465c1cfc161ca783ea"
assert git("diff", "--name-only", OLD, NEW) == "test/cmd/test_app/BUILD"
for arm, src in (("original",OLD),("revised",NEW)):
    git("switch", "--quiet", "--detach", src)
    assert git("rev-parse","HEAD") == src and not git("status","--porcelain","--untracked-files=no")
    rows = R["arms"][arm] = {}
    labels = ("public",) if src == NEW else ("nogo", "public", "prctl", "boot")
    for label in labels:
        row = rows[label] = {"source": src,"status":"INCOMPLETE"}
        save()
        try:
            phase(arm,label,src,row)
            row["status"] = "PASS"
        except Exception as exc:
            row["error"] = {"type":type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
            row["status"] = "FAIL"
            R["errors"][arm+"_"+label] = row["error"]
        save()
R["status"] = "FAIL" if R["errors"] else "PASS"
save()
assert R["status"] == "PASS"
