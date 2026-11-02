"""Qualify the supplier's shell-free goimports diff on native Linux."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

BASE = "0f433775b36d0f73ebb153029747fd4fa5318de3"
SOURCE = "d140e4d63b51cb5053d57cde308379069b5a6436"
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
OUT = {"source": SOURCE, "control": BASE, "verdict": "INCOMPLETE", "phases": {}}


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


def main():
    WORK.mkdir()
    REPORT.mkdir()
    OUT["goVersion"] = subprocess.check_output(["go", "version"], env=ENV).decode().strip()
    require(OUT["goVersion"] == "go version go1.27.3 linux/amd64", "wrong Go toolchain")
    OUT["patchExecutable"] = shutil.which("patch")
    OUT["patchVersion"] = subprocess.check_output([OUT["patchExecutable"], "--version"]).decode().splitlines()[0]
    require(OUT["patchVersion"].startswith("GNU patch "), "not the GNU patch oracle")
    require(subprocess.check_output(["git", "rev-parse", SOURCE + "^"]).decode().strip() == BASE,
            "control is not the candidate parent")
    for arm, source in [("control", BASE), ("candidate", SOURCE)]:
        try:
            checkout(source)
            if arm == "candidate":
                row, p = run("owner_tests", ["go", "test", "-count=1", "-json", "./cmd/goimports", "./internal/diff"])
                events = [json.loads(s) for s in p.stdout.splitlines() if s.startswith(b"{")]
                row["cases"] = [{k: e[k] for k in ("Package", "Test", "Action", "Elapsed") if k in e}
                                for e in events if e.get("Test") and e["Action"] in ("pass", "fail", "skip")]
                row["packages"] = [{"name": e["Package"], "status": e["Action"]} for e in events
                                   if not e.get("Test") and e["Action"] in ("pass", "fail", "skip")]
                if p.returncode:
                    row["failureOutput"] = "".join(e.get("Output", "") for e in events)[-4000:]
                row["verified"] = p.returncode == 0 and len(row["packages"]) == 2 and {
                    e["name"]: e["status"] for e in row["packages"]} == {
                        "golang.org/x/tools/cmd/goimports": "pass", "golang.org/x/tools/internal/diff": "pass",
                    } and {e["Test"] for e in row["cases"] if e["Package"].endswith("/cmd/goimports") and e["Action"] == "pass"} == {"TestCmd", "TestDiff"}
                row["verified"] &= all(e["Action"] == "pass" for e in row["cases"])
                save()
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
    expected = {"owner_tests", "candidate_stdin_no_tmp"} | {
        arm + "_" + phase for arm in ("control", "candidate") for phase in ("build", "patch", "clean", "changed", "invalid")}
    require(not any(k in OUT for k in ("controlError", "candidateError"))
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
