"""Check which compiler and target platform produce the two public static race binaries."""

import hashlib
import json
import os
from pathlib import Path
import subprocess

SRC = "24b7f9c53def5a1c8f29ddffaa54a8d7bf0e8125"
RACE = ["//runsc:runsc-race", "//runsc/cmd/sentry:gvisor_sentry-race"]
QEMU = "@qemu_user_toolchains//:qemu_linux_aarch64_on_linux_x86_64"
GNU = "@@rules_cc++cc_configure+local_config_cc_toolchains//:all"
CC = "@bazel_tools//tools/cpp:current_cc_toolchain"
STANDARD = "//tools/bazeldefs:linux_amd64"
CUSTOM = "@llvm//platforms:linux_x86_64_gnu.2.28"
WORK = Path(os.environ["RUNNER_TEMP"]) / "race-toolchain-control"
FORMAT = WORK / "examine.cquery"
FLAGS = ["--config=race", "-c", "opt", "--jobs=4", "--repo_env=CC=/usr/bin/gcc", "--remote_executor=", "--remote_cache=", "--disk_cache=", "--noshow_progress", "--color=no", "--curses=no"]
report = {"source": SRC, "arms": {}}


def save():
    (WORK / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")


def invoke(key, arm, cmd, options, *args):
    log_path = WORK / f"{key}-{cmd}-{len(arm)}.log"
    with log_path.open("w") as fp:
        result = subprocess.run(["bazelisk", "--nosystem_rc", "--nohome_rc", cmd, *FLAGS, *options, *args], stdout=fp, stderr=subprocess.STDOUT)
    log = log_path.read_text(errors="replace")
    arm[key + "_" + cmd + "_exit"] = result.returncode
    if result.returncode:
        errors = [line[:420] for line in log.splitlines() if "ERROR:" in line or "Error " in line or "Error:" in line or "line " in line]
        arm["diagnostic"] = errors[:9]
    save()
    return result.returncode, log


def measure(name, options):
    row = report["arms"][name] = {}
    code, output = invoke("compiler", row, "cquery", options, "--output=starlark", "--starlark:file=" + str(FORMAT), CC)
    if code:
        return row
    rows = [json.loads(l[len("MEASURE "):]) for l in output.splitlines() if l.startswith("MEASURE ")]
    if len(rows) != 1:
        row["compiler_row_count"] = len(rows)
        save()
        return row
    row["compiler"] = rows[0]
    code, output = invoke("producers", row, "cquery", options, "--output=starlark", "--starlark:file=" + str(FORMAT),
                          'filter("(_impl|_musl)$", deps(set(%s), 2))' % " ".join(RACE))
    row["producers"] = [json.loads(l[len("MEASURE "):]) for l in output.splitlines() if l.startswith("MEASURE ")]
    save()
    return row


def link(name, options):
    row = report["arms"][name]
    if not row.get("producers"):
        return
    code, _ = invoke("public", row, "build", options, "--keep_going", *RACE)
    if code:
        return
    row["outputs"] = {}
    for label in RACE:
        _, out = invoke(label.rsplit(":", 1)[1], row, "cquery", options, "--output=files", label)
        paths = [Path(l) for l in out.splitlines() if l.startswith("bazel-out/") and Path(l).is_file()]
        assert len(paths) == 1, paths
        file, = paths
        phdrs = subprocess.check_output(["readelf", "-hl", str(file)], text=True)
        with file.open("rb") as fp:
            sha = hashlib.file_digest(fp, "sha256").hexdigest()
        row["outputs"][label] = {"file": str(file), "bytes": file.stat().st_size, "sha256": sha,
                                 "x86_64": "Advanced Micro Devices X86-64" in phdrs,
                                 "interpreter": any(l.split()[:1] == ["INTERP"] for l in phdrs.splitlines())}
        save()


assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SRC
assert os.uname().machine == "x86_64"
FORMAT.write_text('''def format(t):
    r = {"label": str(t.label), "platform": [str(v) for v in build_options(t).get("//command_line_option:platforms", [])]}
    for p in providers(t).values():
        c = getattr(p, "cc", p)
        if hasattr(c, "toolchain_id"):
            r.update({"id": c.toolchain_id, "compiler": c.compiler, "libc": c.libc})
    return "MEASURE " + json.encode(r)
''')
cases = {
    "native_default": [], "project": ["--config=x86_64"], "qemu": ["--config=x86_64", "--extra_toolchains=" + QEMU],
    "gcc": ["--config=x86_64", "--extra_toolchains=" + GNU],
    "custom": ["--platforms=" + CUSTOM], "qemu_custom": ["--platforms=" + CUSTOM, "--extra_toolchains=" + QEMU],
    "arm64_qemu": ["--config=aarch64", "--extra_toolchains=" + QEMU],
}
for key, flags in cases.items():
    measure(key, flags)

for key in ("qemu", "gcc"):
    link(key, cases[key])

for key, row in report["arms"].items():
    assert row["compiler"]["compiler"] == ("gcc" if key == "gcc" else "clang"), key
    musl = key in ("native_default", "project", "qemu", "arm64_qemu")
    implemented = [item for item in row["producers"] if item["label"].endswith("_impl")]
    wrappers = [item for item in row["producers"] if item["label"].endswith("_musl")]
    assert len(implemented) == 2 and len(wrappers) == (2 if musl else 0), (key, row["producers"])
    platform = "linux_aarch64_musl" if key == "arm64_qemu" else "linux_x86_64_musl" if musl else CUSTOM.rsplit(":", 1)[1] if "custom" in key else "linux_amd64"
    assert all(len(item["platform"]) == 1 and item["platform"][0].endswith(":" + platform) for item in implemented), key
assert report["arms"]["project"]["compiler"] == report["arms"]["qemu"]["compiler"]
assert report["arms"]["gcc"]["compiler"]["id"] != report["arms"]["qemu"]["compiler"]["id"]
for key in ("qemu", "gcc"):
    assert set(report["arms"][key]["outputs"]) == set(RACE)
    assert all(o["x86_64"] and not o["interpreter"] for o in report["arms"][key]["outputs"].values())
report["complete"] = True
save()
