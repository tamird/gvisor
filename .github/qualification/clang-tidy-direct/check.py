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
import sys
import tarfile
import zipfile

ROOT = Path(os.environ["RUNNER_TEMP"])
OUT = ROOT / "clang-tidy-direct.json"
sys.path.insert(0, str(Path("test/rbe").resolve()))
from image_workload import frontend_record

PRODUCT = "96570700efb2d394b24af1fd8af70cffd8797fa6"
WORKER = "mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
PKG = "//tools/clang_tidy/testdata:"
ASPECT = "//tools/clang_tidy:"
DATA = {"status": "INCOMPLETE", "product": PRODUCT, "expected": ["scratch-first", "scratch-second", "negative"], "phases": {}}


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
    rc = run("git", "show", PRODUCT + ":.bazelrc").stdout.decode()
    for flag in (" --cxxopt=-Wno-error=maybe-uninitialized", " --host_cxxopt=-Wno-error=maybe-uninitialized"):
        check(rc.count(flag) == 1, "unsupported standalone warning flag count")
        rc = rc.replace(flag, "")
    check(Path(".bazelrc").read_text() == rc, "unexpected compiler configuration change")
    prior_path = ROOT / "original-direct.zip"
    check(hashlib.sha256(prior_path.read_bytes()).hexdigest() == "ede65f33b6785f34dc6170dde6b8c136c032e2a5092028961a41a5678023d768", "wrong baseline artifact")
    with zipfile.ZipFile(prior_path) as archive:
        prior = json.loads(archive.read("clang-tidy-direct.json"))
    check(prior["status"] == "PASS" and prior["product"] == "d4ba96a236489d35c331cc8f752dd374e78b3d66", "original qualified source")
    DATA["original"] = {"artifact":"https://github.com/tamird/gvisor/actions/runs/41592065884/artifacts/13211235097", "baseline_clean_sha256":prior["phases"]["local"]["sha256"]}
    checked = run("docker","pull","--platform=linux/amd64",WORKER)
    check(checked.returncode == 0,"cannot fetch pinned worker image for inventory")
    image = ROOT / "observed-one-file.tar"
    check(run("docker","image","save","-o",str(image),WORKER).returncode == 0,"cannot export pinned worker image for inventory")
    with tarfile.open(image) as outer:
        manifest, = json.load(outer.extractfile("manifest.json"))
        layer, = manifest["Layers"]
        with tarfile.open(fileobj=outer.extractfile(layer),mode="r|*") as tree:
            paths=[m.name.lstrip("./") for m in tree if m.isfile() or m.islnk() or m.issym()]
        check(paths==["hello"],"worker contains files besides /hello")
        DATA["worker_image"]={"reference":WORKER,"files":paths,"layers":1}
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
            tgt = "indirect_include" if want_fail else "direct_include"
            asp = "clang_tidy.bzl"
            exlog, bep = captured / (name + ".log.json"), captured / (name + ".bep")
            mode = "remote"
            suffix = "-first" if name == "scratch-first" else "-second"
            flags = ["--config=x86_64", "--noannounce_rc", "--extra_toolchains=@llvm//toolchain:all",
                     "--remote_default_exec_properties=Arch=amd64", "--remote_default_exec_properties=OSFamily=linux",
                     "--remote_default_exec_properties=container-image=docker://" + WORKER,
                     "--remote_default_exec_properties=workload-isolation-type=oci",
                     "--remote_default_exec_properties=recycle-runner=false",
                     "--spawn_strategy=local", "--strategy=ClangTidy=" + mode, "--modify_execution_info=^ClangTidy$=+no-cache", "--remote_download_outputs=toplevel",
                     "--platform_suffix=" + suffix, "--execution_log_json_file=" + str(exlog),
                     "--build_event_json_file=" + str(bep), "--build_event_json_file_path_conversion=false",
                     "--aspects=" + ASPECT + asp + "%clang_tidy", "--output_groups=clang_tidy"]
            res = run(*bazel, "build", *flags, PKG + tgt)
            text = (res.stdout + res.stderr).decode(errors="replace")
            row.update(native_exit=res.returncode, execution_finished=now())
            # Keep logs private. On failure publish only a bounded, secret-checked diagnostic projection.
            row["messages"] = [s[-650:] for s in text.splitlines() if re.search(r"(not found|not created|[Nn]o such file|[Pp]ermission denied|ERROR:|error:|warning:.*(?:tidy|misc-include-cleaner)|FAIL:|TIDY_QUAL_COMPILER)", s)][:8]
            events = [json.loads(s) for s in bep.read_text().splitlines()] if bep.exists() else []
            row["invocation"] = next((e["started"]["uuid"] for e in events if "started" in e), None)
            finished = [e["finished"]["exitCode"].get("code", 0) for e in events if "finished" in e]
            check(finished == [res.returncode], "BEP and native exit differ")
            actions = [s for s in spawns(exlog) if s.get("mnemonic") == "ClangTidy"] if exlog.exists() else []
            check(len(actions) == 1, "expected one executed ClangTidy action")
            s = actions[0]; argv = s["commandArgs"]; prog = argv[0]
            row.update(runner=s.get("runner"),action_exit=s.get("exitCode",0),spawn_status=s.get("status",""))
            elf=run("docker","exec","tidy-client","readelf","-lW",str(root / prog))
            if elf.returncode == 0:
                row["requested_interpreters"] = re.findall(r"Requesting program interpreter: ([^\]]+)",elf.stdout.decode())
            check(prog.endswith("/bin/clang-tidy") or Path(prog).name == "clang-tidy", "action does not execute clang-tidy directly")
            check(s.get("runner") == mode and not s.get("cacheHit"), "wrong actual strategy or cached action")
            if mode == "remote":
                props = {v["name"]:v["value"] for v in s["platform"]["properties"]}
                row["worker"] = {k:props.get(k) for k in ("container-image", "workload-isolation-type", "Arch")}
                check(row["worker"]["workload-isolation-type"] == "oci" and row["worker"]["container-image"] == "docker://" + WORKER, "lint action did not use the observed one-file worker")
            output = argv[argv.index("-serialize-diagnostics") + 1]
            check(output.endswith("/frontend.dia") and output in s["listedOutputs"], "declared diagnostic output missing")
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
                proof = next(v["digest"] for v in s.get("actualOutputs", []) if v["path"] == output)
                full_observer = frontend_record(output, {"bytes":int(proof["sizeBytes"]), "sha256":proof["hash"]}, lambda unused_path:data)
                outsha = full_observer["sha256"]
                spelled = diagnostics(path)
                row.update(sha256=outsha, size=len(data), diagnostics=spelled)
                check(not spelled, "unexpected clean frontend diagnostic")
            row["status"] = "PASS"
        except (ValueError, OSError, KeyError, StopIteration, TypeError) as exc:
            row["error"] = str(exc)
        row["verdict_at"] = now()
        save()
    check(all(row["status"] == "PASS" for row in DATA["phases"].values()), "one or more phases did not qualify")
    a, b = (DATA["phases"][k] for k in ("scratch-first","scratch-second"))
    check(a["output"] != b["output"] and a["sha256"] == b["sha256"] and a["executable_sha256"] == b["executable_sha256"], "different output paths changed clean bytes")
    check(a["sha256"] == DATA["original"]["baseline_clean_sha256"], "one-file worker content differs from prior local/GNU diagnostics")
    DATA.update(status="PASS", verdict_at=now()); save()


if __name__ == "__main__":
    main()
