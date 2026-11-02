"""Qualify the BCR LuaJIT targets on their existing native host matrix."""

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys


SOURCE = "b71adb61cb76def6e12e54b5deb73c1d910f71b5"
VERSION = "2.1.20230430"
CLIENTS = ("9.3.1", "7.7.0", "8.8.1")
ROOT = Path(os.environ["GITHUB_WORKSPACE"]) / "supplier"
PUBLIC = Path(os.environ["RUNNER_TEMP"]) / "luajit-public"
ROW = {"supplier": SOURCE, "runner": os.environ.get("NATIVE_RUNNER"),
       "host": dict(system=platform.system(), machine=platform.machine()),
       "phases": {}, "status": "INCOMPLETE"}


def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    data = json.dumps(ROW, indent=2).encode()
    token = os.environ.get("GITHUB_TOKEN", "").encode()
    assert not token or token not in data
    assert not re.search(rb"github_pat_|gh[pousr]_[A-Za-z0-9]{20}|authorization: bearer", data, re.I)
    PUBLIC.mkdir(exist_ok=True)
    (PUBLIC / "result.json").write_bytes(data + b"\n")


def command(args, row, *, version="9.3.1", timeout=900):
    start = now()
    env = dict(os.environ, USE_BAZEL_VERSION=version)
    proc = subprocess.run(args, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout)
    text = re.sub(r"\x1b\[[0-9;]*m", "", proc.stderr.decode(errors="replace") + "\n" + proc.stdout.decode(errors="replace"))
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if re.search(r"(?i)error|fail|fatal|exception", line)]
    selected = sorted({i for start in starts[:4] for i in range(start, min(start + 4, len(lines)))})
    record = {"args": args, "exit": proc.returncode, "started": start, "finished": now(),
              "stdout_sha256": hashlib.sha256(proc.stdout).hexdigest(),
              "stderr_sha256": hashlib.sha256(proc.stderr).hexdigest(),
              "diagnostics": [lines[i][:500] for i in selected] if proc.returncode else []}
    row.setdefault("commands", []).append(record)
    save()
    assert proc.returncode == 0, f"command exited {proc.returncode}"
    return proc.stdout.decode()


def phase(name, fn):
    row = {"started": now(), "status": "INCOMPLETE"}
    ROW["phases"][name] = row
    try:
        fn(row)
        row["status"] = "PASS"
    except Exception as exc:
        row.update(status="FAIL", error=f"{type(exc).__name__}: {exc}"[:600])
    row["finished"] = now()
    save()
    return row["status"] == "PASS"


def prepare(row):
    assert BAZEL, "native Bazel/Bazelisk is missing"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip() == SOURCE
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)
    expected = {"macos-15-intel": ("Darwin", "x86_64"), "macos-15": ("Darwin", "arm64"),
                "windows-2022": ("Windows", "AMD64")}[os.environ["NATIVE_RUNNER"]]
    assert (platform.system(), platform.machine()) == expected
    row["runner"] = os.environ["NATIVE_RUNNER"]
    row["module_sha256"] = hashlib.sha256((ROOT / "MODULE.bazel").read_bytes()).hexdigest()
    row["overlay_sha256"] = {name: hashlib.sha256((ROOT / "modules/luajit" / VERSION / "overlay" / name).read_bytes()).hexdigest()
                             for name in ("BUILD.bazel", "buildvm.bzl")}


def metadata(row):
    command([BAZEL, "run", "//tools:update_integrity", "--", "luajit", f"--version={VERSION}"], row)
    command(["git", "diff", "--exit-code", "--", "modules/luajit"], row)
    # Upstream distributes Git source only, not release archives. Retain the
    # unwaived original exit independently of this documented BCR exception.
    row["exception"] = {"name": "url_stability", "upstream": "https://luajit.org/download.html",
                        "original": "https://github.com/tamird/gvisor/actions/runs/41535438504/artifacts/13184711518"}
    command([BAZEL, "run", "//tools:bcr_validation", "--", "--check", f"luajit@{VERSION}",
             "--skip_validation=url_stability"], row)


def consumer(row):
    # Use the existing checkout as a minimal caller, without the registry's
    # development dependencies raising the supplier's dependency versions.
    # The module records and overlay stay untouched.
    path = ROOT / "MODULE.bazel"
    text = f'module(name = "luajit_native_test")\nbazel_dep(name = "luajit", version = "{VERSION}")\n'
    path.write_text(text)
    row["module"] = text
    row["registry"] = ROOT.as_uri()


def targets(row, version):
    row["bazel_version"] = command([BAZEL, "--version"], row, version=version).strip()
    assert row["bazel_version"] == "bazel " + version
    opts = ["--registry=" + ROOT.as_uri(), "--registry=https://bcr.bazel.build",
            "--noshow_progress", "--color=no", "--curses=no", "--jobs=3"]
    command([BAZEL, "build", *opts, "@luajit//..."], row, version=version)
    for mode in ([True, False] if platform.system() == "Windows" else [True]):
        flag = "--@luajit//:gc64=" + str(mode).lower()
        command([BAZEL, "build", *opts, flag, "@luajit//:bin/luajit"], row, version=version)
        files = command([BAZEL, "cquery", *opts, flag, "--output=files", "@luajit//:bin/luajit"], row, version=version).splitlines()
        [name] = [name for name in files if Path(name).name in ("luajit", "luajit.exe")]
        row.setdefault("outputs", []).append(files)
        binary = ROOT / name
        data = binary.read_bytes()
        system = "Windows" if platform.system() == "Windows" else "OSX"
        arch = "arm64" if platform.machine() == "arm64" else "x64"
        script = (f"assert(jit.os=='{system}' and jit.arch=='{arch}'); "
                  f"assert(require('ffi').abi('gc64')=={str(mode).lower()}); "
                  "assert(select(2,load('OK')):match('OK')); "
                  "local n=0; for i=1,100000 do n=n+i end; assert(n==5000050000); print('OK')")
        output = command([str(binary), "-e", script], row, version=version, timeout=30)
        assert output.strip() == "OK"
        row.setdefault("executables", []).append({"gc64": mode, "sha256": hashlib.sha256(data).hexdigest(),
                                                   "size": len(data), "jit_os": system, "jit_arch": arch, "stdout": output})


if sys.argv[1:] == ["summary"]:
    rows = {}
    for path in Path(sys.argv[0]).parent.parent.parent.joinpath("native-results").glob("*/result.json"):
        data = json.loads(path.read_bytes())
        name = data["runner"]
        assert path.parent.name == "luajit-native-" + name
        assert name not in rows
        rows[name] = data
    expected = {"macos-15-intel", "macos-15", "windows-2022"}
    ok = set(rows) == expected and all(row["status"] == "PASS" and row["supplier"] == SOURCE for row in rows.values())
    text = "LuaJIT native supplier qualification\n\n" + "\n".join(
        f"- {name}: " + (", ".join(f"{key}={value['status']}" for key, value in rows[name]["phases"].items()) if name in rows else "MISSING")
        for name in sorted(expected)) + "\n"
    Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(text)
    print(text)
    raise SystemExit(not ok)

BAZEL = shutil.which("bazelisk") or shutil.which("bazel")
if phase("prepare", prepare):
    if os.environ["NATIVE_RUNNER"] == "macos-15-intel":
        phase("metadata", metadata)
    if phase("consumer", consumer):
        for version in CLIENTS:
            if not phase(version, lambda row: targets(row, version)):
                break  # Keep the first concrete native failure; do not repeat it.
required = {"prepare", "consumer", *CLIENTS}
if os.environ["NATIVE_RUNNER"] == "macos-15-intel":
    required.add("metadata")
ROW["missing"] = sorted(required - ROW["phases"].keys())
ROW["status"] = "PASS" if not ROW["missing"] and all(row["status"] == "PASS" for row in ROW["phases"].values()) else "FAIL"
ROW["finished"] = now()
save()
# The single summary job reports all native failures together.
