"""Check the actual systrap cross-architecture artifacts inside the builder."""
import datetime
import hashlib
import json
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import traceback

evidence = Path("/evidence")
root = Path("pkg/sentry/platform/systrap/sysmsg")
labels = ["//" + str(root) + ":" + name for name in (
    "sighandler_object", "sighandler_binary", "sighandler_binary_arch", "sighandler_go_arch")]
expected = {"native build-essential", "native C compiler", "foreign packages", "foreign tools",
            "Bazel build", "ARM64 ELF", "split ARM64 binary", "distinct split binaries",
            "ARM64 offsets", "AMD64 offsets", "declared link and object tools"}
now = lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
result = {"start": now(), "checks": {}, "targets": labels, "host": platform.machine(),
          "source": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}

def check(label, ok, details):
    result["checks"][label] = {"ok": bool(ok), "details": details}

def package(name):
    p = subprocess.run(["dpkg-query", "-W", "-f=${db:Status-Status} ${Version}", name],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return p.stdout.strip() if p.returncode == 0 else None

def digest(data):
    return hashlib.sha256(data).hexdigest()

try:
    assert result["host"] == "x86_64", result["host"]
    native = package("build-essential")
    check("native build-essential", native and native.startswith("installed "), native)
    gcc = shutil.which("gcc")
    check("native C compiler", gcc and subprocess.check_output([gcc, "-dumpmachine"], text=True).strip() == "x86_64-linux-gnu", gcc)
    packages = {p: package(p) for p in ("crossbuild-essential-amd64", "crossbuild-essential-arm64",
                                         "gcc-aarch64-linux-gnu", "g++-aarch64-linux-gnu", "binutils-aarch64-linux-gnu")}
    check("foreign packages", all(not s or not s.startswith("installed ") for s in packages.values()), packages)
    tools = {p: shutil.which("aarch64-linux-gnu-" + p) for p in ("gcc", "g++", "as", "ld", "nm", "objcopy")}
    check("foreign tools", not any(tools.values()), tools)
    assert all(v["ok"] for v in result["checks"].values()), result["checks"]
    cmd = ["bazel", "--batch", "build", "--config=aarch64", "--spawn_strategy=local", "--jobs=4",
           "--subcommands", "--verbose_failures", "--noshow_progress", *labels]
    result["build"] = {"started": now(), "argv": cmd}
    with (evidence / "bazel.log").open("wb") as log:
        finished = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    result["build"].update({"exit": finished.returncode, "finished": now()})
    check("Bazel build", finished.returncode == 0, finished.returncode)
    assert finished.returncode == 0, finished.returncode
    out = Path("bazel-bin") / root
    names = ("sighandler.built-in.bin.o", "sighandler.built-in.bin",
             "sighandler.built-in.arm64.bin", "sighandler.built-in.amd64.bin",
             "sighandler_arm64.go", "sighandler_amd64.go")
    data = {n: (out / n).read_bytes() for n in names}
    result["outputs"] = {n: {"bytes": len(b), "sha256": digest(b)} for n, b in data.items()}
    elf = data[names[0]]
    machine = struct.unpack_from("<H", elf, 18)[0]
    check("ARM64 ELF", elf[:6] == b"\x7fELF\x02\x01" and machine == 183, machine)
    (evidence / "elf.txt").write_bytes(subprocess.check_output(["readelf", "-h", out / names[0]]))
    check("split ARM64 binary", data[names[1]] and data[names[1]] == data[names[2]], digest(data[names[2]]))
    check("distinct split binaries", data[names[3]] and data[names[2]] != data[names[3]], digest(data[names[3]]))
    for n, arch in ((names[4], "ARM64"), (names[5], "AMD64")):
        symbols = [l for l in data[n].decode().splitlines() if l.startswith("var Sighandler_blob_offset__")]
        check(arch + " offsets", len(symbols) > 0, symbols)
    log = (evidence / "bazel.log").read_text(errors="replace")
    markers = {t: t in log for t in ("ld.lld", "llvm-nm", "llvm-objcopy")}
    check("declared link and object tools", all(markers.values()), markers)
except Exception:
    result["error"] = traceback.format_exc()
finally:
    result["missing"] = sorted(expected - result["checks"].keys())
    result["finished"] = now()
    result["ok"] = "error" not in result and not result["missing"] and all(v["ok"] for v in result["checks"].values())
    print(json.dumps(result, indent=2))
    (evidence / "result.json").write_text(json.dumps(result, indent=2) + "\n")
raise SystemExit(0 if result["ok"] else 1)
