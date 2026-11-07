"""Resolve the actual vDSO as a gVisor module dependency on an AMD64 client."""

from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys

BASE = "4bee07965270dc7cb4ed89eb6a3b17968b3e0a90"
FIX = "69e89d7a3dfc6b2b484f7dc01a68fad68741dd03"
TARGET = "@gvisor//vdso:vdso.so"
ARCH = "--platforms=@gvisor//tools/bazeldefs:linux_arm64"
SYMBOLS = {"__kernel_clock_getres", "__kernel_clock_gettime", "__kernel_gettimeofday", "__kernel_rt_sigreturn"}
SRC = Path.cwd()
AREA = Path(os.environ["RUNNER_TEMP"]) / "downstream-llvm"
CONSUMER = AREA / "consumer"
REPORT = {"coordinator": platform.machine(), "target": TARGET, "platform": ARCH, "cases": {}}


def save():
    (AREA / "result.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=SRC, text=True).strip()


def call(case, name, argv, timeout=420):
    try:
        p = subprocess.run(argv, cwd=CONSUMER, capture_output=True, timeout=timeout)
        code, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as exc:
        code, out, err = 124, exc.stdout or b"", exc.stderr or b""
    row = {"exit": code, "stdoutSHA256": sha256(out).hexdigest(), "stderrSHA256": sha256(err).hexdigest()}
    lines = err.decode(errors="replace").splitlines()
    if code:
        messages = [s for s in lines if re.search(r'ERROR:|Error in |no such package|tag class|no matching toolchains', s, re.I)]
        row["diagnostics"] = [s.replace(str(AREA), "<job>").replace(str(SRC), "<source>")[:650]
                              for s in messages[:5]]
    case[name] = row
    save()
    return row, out


def bazel(case, name, operation, *args, timeout=420):
    return call(case, name, [str(AREA / "bazelisk"), "--nosystem_rc", "--nohome_rc", "--noworkspace_rc",
                             operation, "--lockfile_mode=off", *args], timeout)


def header(case):
    row, out = bazel(case, "headers", "mod", "show_repo", "--base_module=gvisor", "@kernel_headers")
    text = out.decode(errors="replace")
    row["generatedRepository"] = row["exit"] == 0 and "kernel_headers" in text
    diagnostic = " ".join(row.get("diagnostics", [])).lower()
    row["missingConfigureTag"] = "configure" in diagnostic and "tag class" in diagnostic


def plan(case):
    row, out = bazel(case, "configured", "aquery", ARCH, "--output=jsonproto",
                     'mnemonic("CppCompile|CppLink", ' + TARGET + ')')
    if row["exit"]:
        return
    graph = json.loads(out)
    actions = []
    for action in graph.get("actions", []):
        args = action.get("arguments", [])
        actions.append({"mnemonic": action["mnemonic"], "compiler": args[0][:250] if args else None,
                        "armTarget": any("aarch64" in a and ("target" in a or a.startswith("aarch64")) for a in args),
                        "toolFromLlvm": bool(args and "external/" in args[0] and "llvm" in args[0].lower()),
                        "target": action.get("targetId")})
    row["actions"] = actions[:12]
    row["counts"] = {k: sum(a["mnemonic"] == k for a in actions) for k in ("CppCompile", "CppLink")}
    save()


def build(case):
    bep = AREA / "vdso.bep"
    row, _ = bazel(case, "build", "build", ARCH, "--jobs=3", "--build_event_publish_all_actions",
                   "--build_event_json_file=" + str(bep), TARGET, timeout=1200)
    if bep.exists():
        owners = []
        for line in bep.open():
            event = json.loads(line)
            action = event.get("action", {})
            label = event.get("id", {}).get("actionCompleted", {}).get("label", "")
            primary = event.get("id", {}).get("actionCompleted", {}).get("primaryOutput", "")
            if (label.endswith("//vdso:vdso.so") or ("/vdso/" in primary and "gvisor+" in primary)) and action.get("type") in ("CppCompile", "CppLink"):
                owners.append({"type": action["type"], "success": action.get("success", False)})
        row["reportedOwnerActions"] = owners[:12]
        save()
    if row["exit"]:
        return
    files, out = bazel(case, "defaultOutput", "cquery", ARCH, "--output=files", TARGET)
    if files["exit"]:
        return
    paths = [CONSUMER / s for s in out.decode().splitlines() if s.endswith("/vdso.so")]
    if len(paths) != 1 or not paths[0].is_file():
        return
    elf = paths[0].read_bytes()
    symbols = subprocess.check_output(["readelf", "--dyn-syms", "--wide", str(paths[0])], text=True)
    edata = elf[:6] == b"\x7fELF\x02\x01"
    row["elf"] = {"sha256": sha256(elf).hexdigest(), "size": len(elf), "littleEndianELF64": edata,
                  "type": struct.unpack_from("<H", elf, 16)[0], "machine": struct.unpack_from("<H", elf, 18)[0],
                  "versionedSymbols": sorted(s for s in SYMBOLS if s + "@@LINUX_2.6.39" in symbols)}
    checked, _ = call(case, "projectELFCheck", [sys.executable, str(SRC / "vdso/check_vdso.py"),
                                                 "--check-data", "--vdso=" + str(paths[0])])
    case["linkedARM64"] = (edata and row["elf"]["type"] == 3 and row["elf"]["machine"] == 183
                           and set(row["elf"]["versionedSymbols"]) == SYMBOLS and checked["exit"] == 0)
    save()


def main():
    assert REPORT["coordinator"] == "x86_64"
    assert git("rev-parse", FIX + "^") == BASE
    assert git("diff", "--name-only", BASE, FIX) == "MODULE.bazel"
    CONSUMER.mkdir()
    module = ('module(name = "downstream")\nbazel_dep(name = "gvisor", version = "0.0.1")\n'
              f'local_path_override(module_name = "gvisor", path = {json.dumps(str(SRC))})\n')
    (CONSUMER / "MODULE.bazel").write_text(module)
    (CONSUMER / "BUILD.bazel").write_text("")
    (CONSUMER / ".bazelversion").write_text("9.3.1\n")
    REPORT["consumerModule"] = module.replace(str(SRC), "<source>")
    for name, source in (("stock", BASE), ("rootPolicy", FIX)):
        git("checkout", "--quiet", "--detach", source)
        assert git("rev-parse", "HEAD") == source
        assert (SRC / "images/default/bazelversion").read_text().strip() == "9.3.1"
        assert 'bazel_dep(name = "llvm", version = "0.8.24")' in (SRC / "MODULE.bazel").read_text()
        case = {"source": source, "gvisorModuleSHA256": sha256((SRC / "MODULE.bazel").read_bytes()).hexdigest()}
        REPORT["cases"][name] = case
        save()
        header(case)
        plan(case)
        if name == "rootPolicy":
            build(case)
        save()
    stock, fixed = REPORT["cases"].values()
    REPORT["passed"] = (stock["headers"]["missingConfigureTag"] and fixed["headers"]["generatedRepository"]
                        and fixed.get("linkedARM64", False)
                        and fixed["configured"].get("counts") == {"CppCompile": 2, "CppLink": 1}
                        and all(a["toolFromLlvm"] for a in fixed["configured"]["actions"]
                                if a["mnemonic"] == "CppCompile"))
    save()
    return int(not REPORT["passed"])


if __name__ == "__main__":
    sys.exit(main())
