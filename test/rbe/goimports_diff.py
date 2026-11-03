"""Qualify the supplier's shell-free goimports diff on native Linux."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

BASE = "aba6dfd3c19d76dddd8dcf6e3b4c1953af8c52a6"
PARENT = "c7fa4e24e698ad352f0e7ebc15f6d4ec6dcef16f"
SOURCE = "264f9bec0b5deeb426c2450977ff41dfca131945"
DIFF = "golang.org/x/tools/internal/diff"
GOIMPORTS = "golang.org/x/tools/cmd/goimports"
WORK = Path(os.environ["RUNNER_TEMP"]) / "goimports-diff-work"
REPORT = Path(os.environ["RUNNER_TEMP"]) / "goimports-diff-result"
ENV = {k: v for k, v in os.environ.items() if k in (
    "PATH", "HOME", "USER", "GOROOT", "GOPATH", "GOCACHE", "GOMODCACHE",
    "TMPDIR", "LANG", "XDG_CACHE_HOME",
)} | {"GOTOOLCHAIN": "local", "GOFLAGS": "-mod=readonly", "GOTELEMETRY": "off"}
INPUTS = {
    "dir/input file.go": "package p\n\nfunc f() {\nprintln(1)\n}",
    "other.go": "package q\n\nvar value=2\n",
    "clean.go": "package p\n",
    "invalid.go": "package p\nfunc broken(\n",
}
FORMATTED = {
    "dir/input file.go": "package p\n\nfunc f() {\n\tprintln(1)\n}\n",
    "other.go": "package q\n\nvar value = 2\n",
}
OUT = {"source": SOURCE, "parent": PARENT, "control": BASE, "verdict": "INCOMPLETE", "phases": {}}


def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    (REPORT / "goimports-diff.json").write_text(json.dumps(OUT, indent=2) + "\n")


def require(value, message):
    if not value:
        raise ValueError(message)


def run(name, args, *, cwd=None, env=ENV, data=None):
    row = OUT["phases"][name] = {"started": now(), "verified": False}
    save()
    start = time.monotonic()
    p = subprocess.run(args, cwd=cwd, env=env, input=data, capture_output=True, timeout=600)
    row.update(exit=p.returncode, finished=now(), elapsed=time.monotonic() - start,
               stdoutSha256=sha256(p.stdout).hexdigest(), stderrSha256=sha256(p.stderr).hexdigest())
    # Keep original exits before interpreting output. No authenticated process is run here.
    if p.returncode:
        row["diagnostic"] = p.stderr.decode(errors="replace")[-4000:]
    save()
    return row, p


def checkout(source):
    subprocess.run(["git", "checkout", "--detach", source], check=True, capture_output=True)
    require(subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip() == source,
            "wrong supplier checkout")
    require(not subprocess.check_output(["git", "status", "--porcelain"]), "supplier tree changed")


def owner(name, args):
    row, p = run(name, ["go", "test", "-count=1", "-json", *args])
    events = [json.loads(s) for s in p.stdout.splitlines() if s.startswith(b"{")]
    row["cases"] = [{k: e[k] for k in ("Package", "Test", "Action", "Elapsed") if k in e}
                    for e in events if e.get("Test") and e["Action"] in ("pass", "fail", "skip")]
    row["packages"] = {e["Package"]: e["Action"] for e in events if not e.get("Test")
                       and e["Action"] in ("pass", "fail", "skip")}
    if p.returncode:
        row["failureOutput"] = "".join(e.get("Output", "") for e in events)[-4000:]
    save()
    return row, events


def negative():
    path = "internal/diff/diff_test.go"
    fixture = subprocess.check_output(["git", "show", PARENT + ":" + path])
    # Only the parent test file is overlaid on stock; restore it before testing the CLI.
    stock = Path(path).read_bytes()
    try:
        Path(path).write_bytes(fixture)
        require(subprocess.check_output(["git", "diff", "--name-only"]).decode().splitlines() == [path],
                "baseline regression overlay changed other source")
        row, events = owner("baseline_regression", ["-run", "^TestUnifiedJoinedEdits$", "./internal/diff"])
        output = "".join(e.get("Output", "") for e in events if e.get("Test") == "TestUnifiedJoinedEdits")
        row["fixtureSha256"] = sha256(fixture).hexdigest()
        row["verified"] = (row["exit"] == 1 and row["packages"] == {DIFF: "fail"}
                           and [(e["Package"], e["Test"], e["Action"]) for e in row["cases"]]
                           == [(DIFF, "TestUnifiedJoinedEdits", "fail")]
                           and "@@ -8,4 +7,4 @@" in output and "@@ -8,4 +8,4 @@" in output)
        save()
    finally:
        Path(path).write_bytes(stock)
    checkout(BASE)


def suite(arm):
    packages = {DIFF, DIFF + "/lcs", DIFF + "/difftest"}
    required = {(DIFF, "TestToUnified"), (DIFF, "TestUnifiedJoinedEdits"), (DIFF + "/lcs", "TestLcsFix"),
                (DIFF + "/difftest", "TestVerifyUnified")}
    if arm == "candidate":
        packages.add(GOIMPORTS)
        required.update({(GOIMPORTS, "TestCmd"), (GOIMPORTS, "TestDiff")})
    row, _ = owner(arm + "_owners", ["./internal/diff/..."] + (["./cmd/goimports"] if arm == "candidate" else []))
    passed = {(e["Package"], e["Test"]) for e in row["cases"] if e["Action"] == "pass"}
    skipped = {(e["Package"], e["Test"]) for e in row["cases"] if e["Action"] == "skip"}
    expected_skips = {(DIFF + "/difftest", "TestVerifyUnified/" + c) for c in ("delete_front", "multiple_replace")}
    row["verified"] = (row["exit"] == 0 and row["packages"] == dict.fromkeys(packages, "pass")
                       and required <= passed and skipped == expected_skips
                       and not any(e["Action"] == "fail" for e in row["cases"]))
    save()


def cli(arm, binary):
    directory = WORK / arm
    directory.mkdir()
    for name, data in INPUTS.items():
        target = directory / name
        target.parent.mkdir(exist_ok=True)
        target.write_text(data)
    temporary = directory / "tmp"
    temporary.mkdir()
    no_path = ENV | {"PATH": "", "TMPDIR": str(temporary)}
    selected = ["dir/input file.go", "clean.go", "other.go"]
    # The control needs its real system diff to establish the output compatibility oracle.
    row, p = run(arm + "_patch", [str(binary), "-format-only", "-d", *selected],
                 cwd=directory, env=no_path if arm == "candidate" else ENV)
    row["diff"] = p.stdout.decode()
    row["systemDiffAvailable"] = arm == "control"
    expected_banners = ["diff -u " + f + ".orig " + f for f in FORMATTED]
    require(p.returncode == 0 and [s for s in row["diff"].splitlines() if s.startswith("diff -u ")] == expected_banners,
            "missing multi-file diff")
    require("\\ No newline at end of file" in row["diff"] and "+\tprintln(1)" in row["diff"],
            "missing meaningful diff content")
    apply = WORK / (arm + "-apply")
    shutil.copytree(directory, apply)
    applied = subprocess.run([OUT["patchExecutable"], "--batch", "-p0"], cwd=apply, input=p.stdout,
                             capture_output=True, env=ENV, timeout=30)
    row["patchExit"] = applied.returncode
    row["patched"] = {f: sha256((apply / f).read_bytes()).hexdigest() for f in FORMATTED}
    row["sourceUnchanged"] = all((directory / f).read_text() == data for f, data in INPUTS.items())
    row["verified"] = applied.returncode == 0 and row["sourceUnchanged"] and all(
        (apply / f).read_text() == data for f, data in FORMATTED.items())
    save()
    require(row["verified"], "diff failed GNU patch/read-only verification")
    for name, files, expected_exit in [
        ("clean", ["clean.go"], 0),
        ("changed", selected, 0 if arm == "candidate" else 2),
        ("invalid", ["invalid.go"], 2),
    ]:
        row, p = run(arm + "_" + name, [str(binary), "-format-only", "-d", *files], cwd=directory, env=no_path)
        row["stdout"] = p.stdout.decode()
        row["stderr"] = p.stderr.decode()
        good = p.returncode == expected_exit
        if name == "clean":
            good &= not p.stdout and not p.stderr
        elif name == "invalid":
            good &= not p.stdout and b"invalid.go:2:" in p.stderr and b"expected" in p.stderr
        elif arm == "control":
            good &= b'exec: "diff": executable file not found in $PATH' in p.stderr
        else:
            good &= row["stdout"] == OUT["phases"]["candidate_patch"]["diff"] and not p.stderr
        good &= all((directory / f).read_text() == data for f, data in INPUTS.items())
        row["verified"] = bool(good)
        save()
        require(good, "wrong CLI " + arm + "/" + name)
    if arm == "candidate":
        row, p = run("candidate_stdin_no_tmp", [str(binary), "-format-only", "-d"], cwd=directory,
                     env=no_path | {"TMPDIR": str(directory / "missing")}, data=b"package p")
        row["stdout"] = p.stdout.decode()
        row["verified"] = p.returncode == 0 and not p.stderr and p.stdout.startswith(
            b"diff -u stdin.go.orig stdin.go\n--- stdin.go.orig\t\n+++ stdin.go\t\n") and b"\\ No newline" in p.stdout
        save()
        require(row["verified"], "stdin still needs a temporary directory")
        # Exercise ordinary import resolution as well as the format-only controls.
        module = directory / "module"
        module.mkdir()
        (module / "go.mod").write_text("module example.com/local\n\ngo 1.26\n")
        source = b"package p\n\nfunc f() { fmt.Println(1) }\n"
        formatted = b'package p\n\nimport "fmt"\n\nfunc f() { fmt.Println(1) }\n'
        (module / "main.go").write_bytes(source)
        # Reproduce the parent's joined-edits shape through goimports itself.
        lines = [f"\tprintln({i})\n" for i in range(1, 12)]
        def program(body):
            return ("package p\n\nfunc joined() {\n" + "".join(body) + "}\n").encode()
        joined = program([f"\tprintln( {i} )\n" if i in (1, 3, 11) else line
                          for i, line in enumerate(lines, start=1)])
        (module / "joined.go").write_bytes(joined)
        applied = WORK / "ordinary-apply"
        shutil.copytree(module, applied)
        sdk_path = str(Path(shutil.which("go", path=ENV["PATH"])).resolve().parent)
        require(shutil.which("diff", path=sdk_path) is None, "SDK path unexpectedly supplies diff")
        row, p = run("candidate_ordinary_import", [str(binary), "-d", "main.go", "joined.go"],
                     cwd=module, env=ENV | {"PATH": sdk_path, "GOPROXY": "off"})
        patch = subprocess.run([OUT["patchExecutable"], "--batch", "-p0"], input=p.stdout, env=ENV,
                               cwd=applied, capture_output=True, timeout=30)
        row["patchExit"] = patch.returncode
        row["hunkHeaders"] = [s.decode() for s in p.stdout.splitlines() if s.startswith(b"@@")]
        row["verified"] = (p.returncode == 0 and not p.stderr and b'+import "fmt"' in p.stdout
                           and "@@ -11,5 +11,5 @@" in row["hunkHeaders"]
                           and patch.returncode == 0 and (module / "main.go").read_bytes() == source
                           and (module / "joined.go").read_bytes() == joined
                           and (applied / "main.go").read_bytes() == formatted
                           and (applied / "joined.go").read_bytes() == program(lines))
        save()


def main():
    WORK.mkdir()
    REPORT.mkdir()
    OUT["goVersion"] = subprocess.check_output(["go", "version"], env=ENV).decode().strip()
    require(OUT["goVersion"] == "go version go1.27.3 linux/amd64", "wrong Go toolchain")
    OUT["patchExecutable"] = shutil.which("patch")
    OUT["patchVersion"] = subprocess.check_output([OUT["patchExecutable"], "--version"]).decode().splitlines()[0]
    require(OUT["patchVersion"].startswith("GNU patch "), "not the GNU patch oracle")
    for tip, parent in [(SOURCE, PARENT), (PARENT, BASE)]:
        require(subprocess.check_output(["git", "rev-parse", tip + "^"]).decode().strip() == parent,
                "wrong Gerrit stack")
    for arm, source in [("control", BASE), ("parent", PARENT), ("candidate", SOURCE)]:
        try:
            checkout(source)
            if arm == "control":
                negative()
            else:
                suite(arm)
            if arm == "parent":
                continue
            binary = WORK / ("goimports-" + arm)
            row, p = run(arm + "_build", ["go", "build", "-o", str(binary), "./cmd/goimports"])
            row["verified"] = p.returncode == 0
            if p.returncode:
                continue
            row["binarySha256"] = sha256(binary.read_bytes()).hexdigest()
            row["source"] = source
            save()
            cli(arm, binary)
            checkout(source)
        except Exception as e:
            OUT[arm + "Error"] = {"type": type(e).__name__, "message": str(e)}
            save()
    expected = {"baseline_regression", "parent_owners", "candidate_owners", "candidate_stdin_no_tmp",
                "candidate_ordinary_import"} | {
        arm + "_" + phase for arm in ("control", "candidate") for phase in ("build", "patch", "clean", "changed", "invalid")}
    require(not any(k in OUT for k in ("controlError", "parentError", "candidateError"))
            and set(OUT["phases"]) == expected and all(r["verified"] for r in OUT["phases"].values()),
            "missing or failed qualification phase")
    OUT["verdict"] = "PASS"


if __name__ == "__main__":
    try:
        main()
    finally:
        OUT["verifiedAt"] = now()
        if REPORT.is_dir():
            save()
