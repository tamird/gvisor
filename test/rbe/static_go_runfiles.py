"""Run the two unchanged runfiles consumers against each static Go producer."""
from datetime import datetime, timezone
from hashlib import sha256
import errno
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

TMP = Path(os.environ["RUNNER_TEMP"]) / "runfiles"
BASE = Path.cwd()
APP = Path("test/cmd/test_app/test_app")
PRCTL = "//test/syscalls/linux:prctl_test"
NOGO = "//test/cmd/test_app:test_app_nogo"
GO = "//runsc/container:lookup_probe"
LABELS = [PRCTL, GO, "//test/cmd/test_app:test_app"]
COMMON = ["--remote_cache=grpcs://remote.buildbuddy.io", "--remote_upload_local_results=false", "--jobs=4"]
CASE = "PrctlTest.OrphansReparentedToSubreaper"
REPORT = {"status": "INCOMPLETE", "arms": {}, "carrier": os.environ["GITHUB_SHA"]}
PROBE = """
go_binary(
    name = "lookup_probe",
    testonly = True,
    srcs = ["lookup_probe.go"],
    data = ["//test/cmd/test_app"],
    deps = ["//pkg/test/testutil"],
    nogo = False,
)
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    (TMP / "result.json").write_text(json.dumps(REPORT, indent=2, sort_keys=True) + "\n")


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def run(cmd, row, name, **kwargs):
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    row[name] = {"exit": result.returncode, "finished": now(), "sha256": sha256(result.stdout).hexdigest()}
    save()
    return result


def bazel(verb, args, row, name):
    # One ordinary anonymous read cache and shared output base on the Linux client.
    log = TMP / (name + ".stderr")
    with log.open("wb") as stderr:
        p = subprocess.run(["bazel", verb, *COMMON, *args], stdout=subprocess.PIPE, stderr=stderr)
    row[name] = {"exit": p.returncode, "finished": now(), "stderr_sha256": digest(log)}
    if p.returncode:
        for line in log.read_text(errors="replace").splitlines():
            if line.startswith("ERROR:") and len(line) < 600:
                print(line, flush=True)
    save()
    require(p.returncode == 0, name + " command failed")
    return p.stdout.decode()


def shape(root, name):
    paths = [APP, APP / "test_app", APP.with_name("test_app_") / "test_app"]
    return {str(p): ("directory" if (root / p).is_dir() else
                     "executable" if (root / p).is_file() and os.access(root / p, os.X_OK) else
                     "missing" if not (root / p).exists() else "other") for p in paths}


def arm(name, source):
    row = REPORT["arms"][name] = {"source": source}
    save()
    subprocess.run(["git", "switch", "--quiet", "--detach", source], check=True)
    b = BASE / "runsc/container/BUILD"
    probe = b.parent / "lookup_probe.go"
    original = b.read_bytes()
    # Bazel-only diagnostic declaration; neither runtime helper nor test_app changes.
    b.write_bytes(b'load("//tools:defs.bzl", "go_binary")\n' + original + PROBE.encode())
    shutil.copy(TMP / "static_go_runfiles.go", probe)
    row["probe"] = digest(probe)
    try:
        bazel("build", LABELS, row, "build")
        out = bazel("cquery", ["set(" + " ".join(LABELS) + ")", "--output=starlark",
                               "--starlark:file=tools/show_paths.bzl"], row, "paths")
        paths = {}
        for line in out.splitlines():
            path, key = line.split()
            require(key in {"test_app", "prctl_test", "lookup_probe"} and key not in paths, "unexpected executable output")
            paths[key] = BASE / path
        require(len(paths) == 3 and all(x.is_file() and os.access(x, os.X_OK) for x in paths.values()), "missing executable")
        row["executables"] = {k: str(p.relative_to(BASE)) for k, p in paths.items()}
        app = paths["test_app"]
        row["producer_sha256"] = digest(app)
        elf = subprocess.check_output(["readelf", "-hl", app], text=True)
        dynamic = subprocess.check_output(["readelf", "-d", app], text=True)
        require("Advanced Micro Devices X86-64" in elf and "INTERP" not in elf and "(NEEDED)" not in dynamic, "not a static amd64 ELF")
        row["static_amd64"] = True
        expected = APP / "test_app" if name == "stock" else APP.with_name("test_app_") / "test_app"
        for k in ["prctl_test", "lookup_probe"]:
            root = Path(str(paths[k]) + ".runfiles") / "_main"
            views = shape(root, name)
            row[k] = {"layout": views}
            require(views[str(APP)] == ("directory" if name == "stock" else "missing") and views[str(expected)] == "executable"
                    and digest(root / expected) == row["producer_sha256"], "wrong runfiles tree or binary identity")
        root = Path(str(paths["lookup_probe"]) + ".runfiles") / "_main"
        dest = TMP / (name + ".copy")
        p = run([paths["lookup_probe"], dest], row, "go_copy", cwd=root)
        copy = json.loads(p.stdout)
        row["go_copy"].update(copy)
        root = Path(str(paths["prctl_test"]) + ".runfiles")
        xml = TMP / (name + ".xml")
        p = run([paths["prctl_test"], "--gtest_filter=" + CASE, "--gtest_output=xml:" + str(xml)],
                row, "cpp", cwd=root / "_main", env=os.environ | {"TEST_SRCDIR": str(root), "RUNFILES_DIR": str(root), "TEST_WORKSPACE": "_main"})
        cases = ET.parse(xml).findall(".//testcase")
        require(len(cases) == 1 and cases[0].get("classname") + "." + cases[0].get("name") == CASE and cases[0].get("status") == "run"
                and cases[0].find("skipped") is None, "missing named native case")
        failures = cases[0].findall("failure")
        msg = "\n".join(x.get("message", "") for x in failures)
        m = re.search(r"execve_errno\s+Which is: (\d+)", msg)
        row["cpp"].update(case=CASE, failures=len(failures), execve_errno=int(m[1]) if m else None, xml_sha256=digest(xml))
        save()
        if name == "stock":
            require(row["go_copy"]["exit"] == 1 and copy == {"path": str(APP), "copy_errno": errno.EISDIR}
                    and p.returncode == 1 and len(failures) == 1 and m and int(m[1]) == errno.EACCES, "unexpected original lookup/exec failure")
        else:
            require(row["go_copy"]["exit"] == 0 and copy == {"path": str(expected), "copy_errno": 0} and digest(dest) == row["producer_sha256"]
                    and p.returncode == 0 and not failures, "corrected runtime did not pass")
            expr = 'outputs(".*/test/cmd/test_app/.*[.]findings", mnemonic("GoStaticAnalysis", deps(' + NOGO + ')))'
            actions = bazel("aquery", [expr, "--include_aspects", "--output=text", "--noinclude_commandline"], row, "nogo_graph")
            inner = sorted(set(re.findall(r'bazel-out/[^\s,\]]+/bin/test/cmd/test_app/[^\s,\]]+/[^\s,\]]+[.]findings\b', actions)))
            require(len(inner) >= 2, "missing transitioned original binary analysis actions")
            row["nogo_graph"]["unfiltered_binary_findings"] = inner
            bep = TMP / "nogo.bep"
            bazel("test", ["--nocache_test_results", "--build_event_json_file=" + str(bep), NOGO], row, "nogo_test")
            attempts = [e["testResult"] for line in bep.read_text().splitlines() if
                        (e := json.loads(line)).get("id", {}).get("testResult", {}).get("label") == NOGO]
            require(len(attempts) == 1 and attempts[0]["status"] == "PASSED" and not attempts[0].get("cachedLocally")
                    and not attempts[0].get("executionInfo", {}).get("cachedRemotely"), "missing fresh Nogo result")
            log = (BASE / "bazel-testlogs/test/cmd/test_app/test_app_nogo/test.log").read_text()
            sections = re.findall(r'^(amd64|arm64)\.\.\.PASS$', log, re.M)
            require(sorted(sections) == ["amd64", "arm64"], "missing original Nogo architecture section")
            row["nogo_test"]["sections"] = sections
        row["observed"] = True
    finally:
        b.write_bytes(original)
        probe.unlink()
        save()


for name, source in [("stock", os.environ["COMPILER"]), ("fixed", os.environ["FIXED"])]:
    try:
        arm(name, source)
    except Exception as exc:
        REPORT["arms"][name]["error"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        save()
REPORT["verified"] = now()
if all(REPORT["arms"][name].get("observed") for name in ("stock", "fixed")):
    REPORT["status"] = "VERIFIED"
save()
raise SystemExit(REPORT["status"] != "VERIFIED")
