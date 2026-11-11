"""Build the installed launchers, Debian packages, and independent Sentry control."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from owner_inspect_elf import notes

TMP = Path(os.environ["RUNNER_TEMP"])
SOURCE = TMP / "version-export"
OUT = TMP / "label-proof"
OUT.mkdir(exist_ok=True)
GROUP = sys.argv[1]
NAME = "release-20261111.0"
OTHER = "release-20261111.1"
LABELS = {"old": NAME, "release": NAME, "development": "(devel)", "different": OTHER}
SENTRY = "//runsc/cmd/sentry:gvisor_sentry"
GOFER = "//runsc/cmd/netgofer:runsc-netgofer"
PROBE = "//test/cmd/test_app:test_app"
FLAGS = ["--config=x86_64", "--jobs=4", "--remote_cache=", "--remote_executor=", "--noshow_progress", "--color=no"]
BAZEL = [TMP / "version-bazel", "--nohome_rc", "--nosystem_rc"]
record = {"group": GROUP, "source": os.environ["BASELINE" if GROUP == "old" else "SOURCE"], "links": {}, "result": "FAIL"}
def text(args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()
def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
def bazel(name, more, *targets, command="build"):
    path = TMP / (GROUP + "-" + name + ".log")
    with path.open("w") as log:
        result = subprocess.run([*BAZEL, command, *FLAGS, *more, *targets], cwd=SOURCE, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        print(path.read_text(errors="replace")[-12000:], flush=True)
    result.check_returncode()
    return path
def sidecar(binary, label, want, succeeds):
    env = {**os.environ, "GVISOR_ENFORCE_RELEASE": want}
    result = subprocess.run([binary, "--platform=systrap", "flags"], env=env, capture_output=True, text=True)
    error = result.stdout + result.stderr
    assert (result.returncode == 0) == succeeds, (binary, label, want, result.returncode, error[-1600:])
    if not succeeds:
        assert "cannot verify the expected release" in error and label in error and want in error, (binary, label, want, error[-1600:])
    return result.returncode
def build():
    more = []
    if GROUP == "old":
        status = TMP / "legacy-named-status"
        status.write_text("#!/bin/sh\nprintf '%s\\n' 'STABLE_VERSION " + NAME + "'\n")
        status.chmod(0o755)
        more = ["--stamp", "--workspace_status_command=" + str(status)]
    elif GROUP in {"release", "different"}:
        more = ["--stamp", "--embed_label=" + LABELS[GROUP]]
    if GROUP == "placeholders":
        for label in ["0.0.0", "VERSION_MISSING", "(devel)"]:
            flags = ["--stamp", "--embed_label=" + label]
            bazel(label, flags, "//runsc", "//tools/bazeldefs:version")
            cli = SOURCE / "bazel-bin/runsc/runsc_/runsc"
            display = text([cli, "--version"]).splitlines()[0]
            assert display == "runsc version (devel)", display
            v = (SOURCE / "bazel-bin/tools/bazeldefs/version.txt").read_text().strip()
            assert v == "0~devel", v
            record["links"][label] = {"display": display, "packageVersionInput": v,
                "strictExit": sidecar(cli, "(devel)", label, False)}
        return
    label = LABELS[GROUP]
    abi = SOURCE / "runsc/cmd/sentry/sentrycmd/boot.go"
    SOURCE.mkdir(exist_ok=True)
    clean = abi.read_bytes()
    for letter in ("AB" if GROUP == "development" else "A"):
        if letter == "B":
            old = b'timer.Reached("CPUID initialized")'
            assert clean.count(old) == 1
            abi.write_bytes(clean.replace(old,b'timer.Reached("CPU identification initialized")'))
        try:
            targets = [SENTRY]
            if letter == "A":
                targets += ["//:release", PROBE]
                if GROUP in {"release", "development"}:
                    targets += ["//debian:debian", "//tools/parsers:parser"]
            bazel(letter, more, *targets)
            exe = TMP / (GROUP+"-"+letter+".sentry")
            shutil.copy2(SOURCE / "bazel-bin/runsc/cmd/sentry/gvisor_sentry_/gvisor_sentry",exe)
            go, gnu = notes(exe)
            r = {"go_id":go,"gnu_id":gnu,"sha256":digest(exe),"targets":targets}
            record["links"][letter] = r
            if letter == "A":
                install = TMP / (GROUP+"-release")
                shutil.copytree(SOURCE / "bazel-bin/release",install)
                assert digest(install / "gvisor-bin/gvisor_sentry") == r["sha256"]
                shutil.copy2(SOURCE / "bazel-bin/test/cmd/test_app/test_app_/test_app",TMP / (GROUP+"-guest"))
                r["cli_sha256"] = digest(install/"runsc")
                r["cli_version"] = text([install/"runsc","--version"]).splitlines()[0]
                assert r["cli_version"] == "runsc version "+label,r
                if GROUP != "old":
                    r["gofer_version"] = text([install/"gvisor-bin/runsc-netgofer","--version"]).splitlines()[0]
                    assert r["gofer_version"] == r["cli_version"]
                    r["enforce"] = {}
                    for file in ("runsc","gvisor-bin/runsc-netgofer"):
                        r["enforce"][file] = {"own": sidecar(install/file,label,label,GROUP!="development"),
                                              "other": sidecar(install/file,label,"release-20000101.0",False)}
                if GROUP in {"release","development"}:
                    r["debVersion"] = text(["dpkg-deb","-f",SOURCE / "bazel-bin/debian/runsc.deb","Version"])
                    assert r["debVersion"] == ("0~devel" if GROUP == "development" else NAME[8:]),r
            elif letter == "B":
                assert go != record["links"]["A"]["go_id"]
                assert r["sha256"] != record["links"]["A"]["sha256"]
        finally:
            if letter == "B":
                abi.write_bytes(clean)
try:
    build()
    record["result"] = "PASS"
finally:
    (OUT / ("build-"+GROUP+".json")).write_text(json.dumps(record,indent=2)+"\n")
