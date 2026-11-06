"""Exercise the unchanged Debian make target and the installed source layout."""

import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile

root = Path.cwd()
tmp = Path(os.environ["TEST_TMPDIR"])
wrapper = root / "vdso" / "check_vdso.py"
assert (root / "go.mod").is_file() and wrapper.is_file()
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
    try:
        probe.connect(("192.0.2.1", 9))
    except OSError as e:
        assert e.errno == errno.ENETUNREACH, e
    else:
        raise AssertionError("the packaging test has an external route")

# The distribution build compiler is older than the version in the project's
# go.mod. Also simulate GOOS/GOARCH exported for a cross-compiled target.
env = dict(os.environ, CGO_ENABLED="0", GO111MODULE="on", GOOS="linux",
           GOARCH="arm64", GOTOOLCHAIN="auto", GOCACHE=str(tmp / "go-cache"))
version = subprocess.check_output(["go", "version"], cwd=tmp, env=dict(os.environ, GOTOOLCHAIN="local"), text=True).strip()
assert version.startswith("go version go1.22.2 linux/amd64")
report = {"go": version, "route": "ENETUNREACH", "cases": {}}

def check(name, args, cwd, valid, expected=""):
    p = subprocess.run(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    report["cases"][name] = {"exit": p.returncode, "data_error": "unexpected nonempty section .data" in p.stdout}
    if (p.returncode == 0) != valid or expected not in p.stdout:
        raise AssertionError(f"{name} returned {p.returncode}: {p.stdout[-1500:]}")

try:
    for tag, valid in (("amd64", True), ("injected", False)):
        binary = tmp / f"debian-{tag}.so"
        local = os.path.relpath(binary, root)
        check("recipe-" + tag, ["make", "--no-print-directory", "-f", "vdso/debian_vdso.mk",
                                "CC=g++", "DEB_HOST_ARCH=" + tag, "VDSO_SO=" + local, local],
              root, valid, "" if valid else "unexpected nonempty section .data")
        image = binary.read_bytes()
        assert image[:4] == b"\x7fELF" and struct.unpack_from("<H", image, 18)[0] == 62
        report["cases"]["recipe-" + tag]["elf_sha256"] = hashlib.sha256(image).hexdigest()
        with tempfile.TemporaryDirectory(dir=tmp) as outside:
            out = Path(outside)
            (out / "go.mod").write_text("module example.invalid/not-the-checker\n\ngo 1.99.0\n")
            relative = "a relative ELF.so"
            shutil.copyfile(binary, out / relative)
            check("external-" + tag, ["python3", str(wrapper), "--check-data", "--vdso=" + relative],
                  out, valid, "" if valid else "unexpected nonempty section .data")
            if not valid:
                check("external-optional-data", ["python3", str(wrapper), "--vdso=" + relative],
                      out, True)
    standalone = tmp / "copied-alone"
    standalone.mkdir()
    shutil.copyfile(wrapper, standalone / wrapper.name)
    check("source-label-alone", ["python3", wrapper.name, "--vdso=../debian-amd64.so"], standalone,
          False, "check/main.go")
finally:
    print("DISTRO-WRAPPER " + json.dumps(report, sort_keys=True), flush=True)
