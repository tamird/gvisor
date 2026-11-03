"""Check the Bazel action, its direct diagnostics and its serialized output."""

import ctypes
import ctypes.util
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(os.environ["RUNNER_TEMP"])
OUT = ROOT / "clang-tidy-direct.json"
PRODUCT = "d4ba96a236489d35c331cc8f752dd374e78b3d66"
PKG = "//tools/clang_tidy/testdata:"
ASPECT = "//tools/clang_tidy:"
DATA = {"status": "INCOMPLETE", "product": PRODUCT, "expected": ["local", "remote"], "phases": {}}


def now():
    return datetime.now(timezone.utc).isoformat()


def check(condition, message):
    if not condition:
        raise ValueError(message)


def save():
    OUT.write_text(json.dumps(DATA, indent=2) + "\n")


def run(*args):
    return subprocess.run(args, capture_output=True, check=False)


def spawns(path):
    # Bazel's JSON execution log is a stream of JSON objects.
    data = path.read_text().strip()
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(data):
        if data[pos].isspace():
            pos += 1
            continue
        item, pos = decoder.raw_decode(data, pos)
        yield item


class CXString(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p), ("private_flags", ctypes.c_uint)]


def diagnostics(path):
    lib = ctypes.CDLL(ctypes.util.find_library("clang-18") or "libclang-18.so.18")
    ptr, u = ctypes.c_void_p, ctypes.c_uint
    for name, args, result in [
        ("clang_loadDiagnostics", [ctypes.c_char_p, ctypes.POINTER(u), ctypes.POINTER(CXString)], ptr),
        ("clang_getNumDiagnosticsInSet", [ptr], u), ("clang_getDiagnosticInSet", [ptr, u], ptr),
        ("clang_getDiagnosticSpelling", [ptr], CXString), ("clang_getCString", [CXString], ctypes.c_char_p),
        ("clang_disposeString", [CXString], None), ("clang_disposeDiagnostic", [ptr], None),
        ("clang_disposeDiagnosticSet", [ptr], None),
    ]:
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, result
    err, msg = u(), CXString()
    ds = lib.clang_loadDiagnostics(os.fsencode(path), ctypes.byref(err), ctypes.byref(msg))
    lib.clang_disposeString(msg)
    check(ds and err.value == 0, "libclang cannot decode the .dia output")
    spellings = []
    for i in range(lib.clang_getNumDiagnosticsInSet(ds)):
        diag = lib.clang_getDiagnosticInSet(ds, i)
        value = lib.clang_getDiagnosticSpelling(diag)
        spellings.append(lib.clang_getCString(value).decode())
        lib.clang_disposeString(value)
        lib.clang_disposeDiagnostic(diag)
    lib.clang_disposeDiagnosticSet(ds)
    return spellings


def main():
    save()
    check(run("git", "rev-parse", "HEAD^").stdout.decode().strip() == PRODUCT, "product parent")
    check(run("git", "diff", "--quiet", PRODUCT, "--", "tools/clang_tidy/clang_tidy.bzl").returncode == 0, "product aspect changed")
    baseline = Path("tools/clang_tidy/clang_tidy.bzl").read_text()
    check(Path("tools/clang_tidy/allowed.bzl").read_text()==baseline.replace("--warnings-as-errors=*", "--warnings-as-errors=-*"), "control aspect differs from production beyond warning severity")
    rc = run("git", "show", PRODUCT + ":.bazelrc").stdout.decode()
    for flag in (" --cxxopt=-Wno-error=maybe-uninitialized", " --host_cxxopt=-Wno-error=maybe-uninitialized"):
        check(rc.count(flag) == 1, "unsupported standalone warning flag count")
        rc = rc.replace(flag, "")
    check(Path(".bazelrc").read_text() == rc, "unexpected compiler configuration change")
    prior_path = ROOT / "original-direct.zip"
    check(hashlib.sha256(prior_path.read_bytes()).hexdigest() == "f5569fe1f6f38a4f9b3d35fd0663608f32d68fd3690a951011dadee777f5e695", "wrong original qualification")
    with zipfile.ZipFile(prior_path) as archive:
        check(archive.namelist() == ["clang-tidy-direct.json"], "original report membership")
        prior = json.loads(archive.read("clang-tidy-direct.json"))
    check(prior["product"] == PRODUCT and prior["bazel"] == "bazel 8.8.1", "wrong original source/tool")
    refs = prior["phases"]
    check(refs["negative"]["status"] == "PASS" and (refs["negative"]["native_exit"],refs["negative"]["action_exit"]) == (1,1), "original fatal action")
    allowed=refs["allowed"]
    check(allowed["action_exit"] == allowed["native_exit"] == 0 and any(re.search(r"compiler_control\.cc:\d+:\d+: warning:.*\[misc-include-cleaner", v) for v in allowed["messages"]), "missing original allowed warning")
    for kind in ("local", "remote", "allowed"):
        diag, = refs[kind]["diagnostics"]
        check("maybe-uninitialized" in diag and "unknown warning option" in diag and refs[kind]["sha256"] == allowed["sha256"], "unexpected original serialized contents")
    DATA["original"]={"url":"https://github.com/tamird/gvisor/actions/runs/41591210365/artifacts/13209814534", "result":prior}
    save()
    captured = ROOT / "tidy-private"
    captured.mkdir()
    bazel = ["docker", "exec", "tidy-client", "bazel", "--bazelrc=" + str(ROOT / "review.bazelrc")]
    version = run("docker", "exec", "tidy-client", "bazel", "--version").stdout.decode().strip()
    DATA["bazel"] = version
    check(version == "bazel 8.8.1", "expected repository Bazel 8.8.1")
    root = Path(run(*bazel, "info", "execution_root").stdout.decode().strip())
    check(root.is_absolute(), "missing execroot")
    for name in DATA["expected"]:
        row = DATA["phases"][name] = {"status": "INCOMPLETE", "started": now()}
        try:
            want_fail = name == "negative"
            tgt = "indirect_include" if want_fail else "compiler_control" if name == "allowed" else "direct_include"
            asp = "allowed.bzl" if name == "allowed" else "clang_tidy.bzl"
            exlog, bep = captured / (name + ".log.json"), captured / (name + ".bep")
            mode = "local" if name == "local" else "remote"
            suffix = "-first" if name == "local" else "-second"
            flags = ["--config=x86_64", "--noannounce_rc", "--extra_toolchains=@llvm//toolchain:all",
                     "--remote_default_exec_properties=Arch=amd64", "--remote_default_exec_properties=OSFamily=linux",
                     "--remote_default_exec_properties=container-image=docker://" + os.environ["GNU"],
                     "--remote_default_exec_properties=workload-isolation-type=firecracker",
                     "--remote_default_exec_properties=recycle-runner=false",
                     "--strategy=ClangTidy=" + mode, "--modify_execution_info=^ClangTidy$=+no-cache", "--remote_download_outputs=toplevel",
                     "--platform_suffix=" + suffix, "--execution_log_json_file=" + str(exlog),
                     "--build_event_json_file=" + str(bep), "--build_event_json_file_path_conversion=false",
                     "--aspects=" + ASPECT + asp + "%clang_tidy", "--output_groups=clang_tidy"]
            res = run(*bazel, "build", *flags, PKG + tgt)
            text = (res.stdout + res.stderr).decode(errors="replace")
            row.update(native_exit=res.returncode, execution_finished=now())
            # Keep logs private. On failure publish only a bounded, secret-checked diagnostic projection.
            row["messages"] = [s[-650:] for s in text.splitlines() if re.search(r"(not found|not created|permission denied|ERROR:|error:|warning:.*(?:tidy|misc-include-cleaner)|FAIL:|TIDY_QUAL_COMPILER)", s)][:8]
            events = [json.loads(s) for s in bep.read_text().splitlines()] if bep.exists() else []
            row["invocation"] = next((e["started"]["uuid"] for e in events if "started" in e), None)
            finished = [e["finished"]["exitCode"].get("code", 0) for e in events if "finished" in e]
            check(finished == [res.returncode], "BEP and native exit differ")
            actions = [s for s in spawns(exlog) if s.get("mnemonic") == "ClangTidy"] if exlog.exists() else []
            check(len(actions) == 1, "expected one executed ClangTidy action")
            s = actions[0]; argv = s["commandArgs"]; prog = argv[0]
            check(prog.endswith("/bin/clang-tidy") or Path(prog).name == "clang-tidy", "action does not execute clang-tidy directly")
            check(s.get("runner") == mode and not s.get("cacheHit"), "wrong actual strategy or cached action")
            if mode == "remote":
                props = {v["name"]:v["value"] for v in s["platform"]["properties"]}
                row["worker"] = {k:props.get(k) for k in ("container-image", "workload-isolation-type", "Arch")}
                check(row["worker"]["workload-isolation-type"] == "firecracker", "unexpected remote isolation")
            output = argv[argv.index("-serialize-diagnostics") + 1]
            check(output.endswith("/diagnostics.dia") and output in s["listedOutputs"], "declared diagnostic output missing")
            action_exit = s.get("exitCode", 0)
            executable = next((v.get("digest", {}).get("hash") for v in s.get("inputs", []) if v.get("path") == prog), None)
            check(bool(executable), "missing declared executable digest")
            row.update(executable_sha256=executable, program=prog, runner=s.get("runner"), cached=bool(s.get("cacheHit")), action_exit=action_exit, output=output)
            if want_fail:
                check(res.returncode != 0 and action_exit != 0 and re.search(r"indirect_include\.cc:\d+:\d+: error:.*\[misc-include-cleaner", text), "no file/line/check from native failure")
            else:
                check(res.returncode == 0 and action_exit == 0 and not s.get("status"), "native clean/control build failed")
                copy = run("docker", "exec", "tidy-client", "cat", str(root / output))
                check(copy.returncode == 0, "cannot read the actual action output")
                path = captured / (name + ".dia")
                data = copy.stdout
                path.write_bytes(data)
                check(data.startswith(b"DIAG"), "not serialized diagnostics")
                outsha = hashlib.sha256(data).hexdigest()
                check(any(o["path"] == output and o["digest"]["hash"] == outsha for o in s.get("actualOutputs", [])), "action result did not produce these bytes")
                spelled = diagnostics(path)
                row.update(sha256=outsha, size=len(data), diagnostics=spelled)
                if name == "allowed":
                    check(any("TIDY_QUAL_COMPILER" in diag for diag in spelled), "compiler warning not serialized")
                    row["tidy_finding_serialized"] = any("no header providing" in diag for diag in spelled)
                    row["tidy_finding_printed"] = bool(re.search(r"compiler_control\.cc:\d+:\d+: warning:.*\[misc-include-cleaner", text))
                    check(row["tidy_finding_printed"], "missing the independently emitted include-cleaner warning")
                else:
                    check(not spelled, "unexpected clean frontend diagnostic")
            row["status"] = "PASS"
        except (ValueError, OSError, KeyError, StopIteration) as exc:
            row["error"] = str(exc)
        row["verdict_at"] = now()
        save()
    check(all(row["status"] == "PASS" for row in DATA["phases"].values()), "one or more phases did not qualify")
    a, b = (DATA["phases"][k] for k in ("local","remote"))
    check(a["output"] != b["output"] and a["sha256"] == b["sha256"] and a["executable_sha256"] == b["executable_sha256"], "different output paths changed clean bytes")
    DATA.update(status="PASS", verdict_at=now()); save()


if __name__ == "__main__":
    main()
