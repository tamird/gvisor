"""Run the existing Bazel clang-tidy selection with native scratch actions."""
from collections import Counter
from pathlib import Path
import hashlib
import json
import os
import subprocess
import struct
import shutil

WORK = Path(os.environ["RUNNER_TEMP"]) / "clang-tidy-arches"
WORKER = "mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
SOURCE = "ccb956b31268a0d9cda723bac9abacaf2008d03a"
REPORT = {"source": SOURCE, "compiler": "8178e3fe5", "targets": ["//test/...", "//tools/..."], "worker": WORKER, "architectures": {}}
ENV = dict(os.environ, PATH=str(WORK / "bin") + ":" + os.environ["PATH"],
           LINT_CACHE_DIR=str(WORK / "lintcache"))


def save():
    (WORK / "result.json").write_text(json.dumps(REPORT, indent=2, sort_keys=True))


def records(path):
    decoder = json.JSONDecoder()
    data = ""
    with path.open() as stream:
        while True:
            data = data.lstrip()
            if data:
                try:
                    item, end = decoder.raw_decode(data)
                except json.JSONDecodeError:
                    pass
                else:
                    data = data[end:]
                    yield item
                    continue
            chunk = stream.read(1024 * 1024)
            if not chunk:
                if data:
                    raise ValueError("Incomplete execution log")
                return
            data += chunk


def read_output(spawn, root):
    files = spawn.get("actualOutputs", [])
    if len(files) != 1:
        return {}
    out = Path(files[0]["path"])
    if out.is_absolute() or ".." in out.parts or out.name != "frontend.dia":
        raise ValueError(f"Unexpected clang-tidy output {out}")
    digest = files[0]["digest"]
    record = {"path": str(out), "sha256": digest["hash"], "bytes": int(digest.get("sizeBytes", 0))}
    data = (root / out).read_bytes()
    record["verified"] = data.startswith(b"DIAG") and len(data) == record["bytes"] and hashlib.sha256(data).hexdigest() == record["sha256"]
    return record


def collect(row, log, bep, executions, root):
    with log.open(errors="replace") as stream:
        row["diagnostics"] = [
            s.strip()[:420] for s in stream
            if s.startswith(("ERROR:", "FATAL:", "WARNING: Remote", "FAILED:"))
            or any(x in s for x in (": error:", ": warning:", "Error downloading", "ClangTidy action failed", "#include ", "Error while processing"))
        ][:160]
    if bep.exists():
        failed = row["failed_actions"] = []
        with bep.open() as stream:
            for line in stream:
                event = json.loads(line)
                if "started" in event:
                    row["invocation"] = event["started"]["uuid"]
                if "finished" in event:
                    row["bazel_exit"] = event["finished"]["exitCode"].get("code", 0)
                act = event.get("action", {})
                if act and not act.get("success", False) and len(failed) < 25:
                    failed.append({"id": event["id"].get("actionCompleted",{}).get("label"),
                                   "mnemonic": act.get("type"), "exit": act.get("exitCode")})
    if not executions.exists():
        return
    actions = row["actions"] = []
    types = Counter()
    try:
        for s in records(executions):
            if s.get("mnemonic") != "ClangTidy":
                continue
            args = s["commandArgs"]
            source = args[args.index("--")-1]
            props = {p["name"]: p["value"] for p in s.get("platform",{}).get("properties",[])}
            tool = args[0]
            flags = [a for a in args if a.startswith(("--target=", "-target=", "--sysroot="))]
            for k in ("-target", "--target"):
                if k in args:
                    flags.append(k + "=" + args[args.index(k)+1])
            types.update([a for a in flags if "target=" in a])
            tool_hash = next((i.get("digest",{}).get("hash") for i in s.get("inputs",[]) if i.get("path") == tool),None)
            action = {"owner": s.get("targetLabel"), "source": source, "flags": flags,
                      "runner": s.get("runner"), "cached": s.get("cacheHit",False), "exit": s.get("exitCode",0),
                      "program": tool, "program_sha256": tool_hash,
                      "scratch": props.get("container-image") == "docker://" + WORKER and props.get("workload-isolation-type") == "oci"}
            actions.append(action)
            if action["exit"] == 0:
                try:
                    action["output"] = read_output(s, root)
                except (KeyError, OSError, ValueError) as err:
                    action["output_error"] = str(err)[:250]
    finally:
        row["target_flags"] = dict(types)
        row["action_count"] = len(actions)
        row["verified_outputs"] = sum(bool(a.get("output",{}).get("verified")) for a in actions)
        row["fresh_scratch"] = sum(a["runner"]=="remote" and a["scratch"] and not a["cached"] for a in actions)
        save()


STUB = "//pkg/sentry/platform/systrap/sysmsg:sighandler_object"
PKG = Path("pkg/sentry/platform/systrap/sysmsg")
def elf(path):
    data = path.read_bytes()
    syms = subprocess.check_output(["nm", str(path)], text=True).splitlines()
    return {"machine": struct.unpack_from("<H", data, 18)[0], "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "memcpy": [s for s in syms if s.rstrip().endswith(" memcpy")],
            "undefined": subprocess.check_output(["nm","-u",str(path)], text=True).splitlines()}
def run(cpu, config, focused=False):
    key = cpu+"-focused" if focused else cpu
    row = REPORT["architectures"][key] = {"status": "INCOMPLETE"}
    log, bep, executions = [WORK / (key + suffix) for suffix in (".log", ".bep", ".spawns")]
    flags = ["--config=" + config, "--compilation_mode=opt", "--jobs=24", "--keep_going", "--lockfile_mode=off",
             "--spawn_strategy=local", "--strategy=ClangTidy=remote", "--modify_execution_info=^ClangTidy$=+no-cache",
             "--remote_default_exec_properties=OSFamily=linux", "--remote_default_exec_properties=Arch=amd64",
             "--remote_default_exec_properties=workload-isolation-type=oci", "--remote_default_exec_properties=container-image=docker://" + WORKER,
             "--remote_default_exec_properties=recycle-runner=false", "--remote_download_outputs=toplevel",
             "--build_event_json_file=" + str(bep), "--execution_log_json_file=" + str(executions)]
    (WORK / "selection.rc").write_text("\n".join("build "+f for f in flags)+"\n")
    with log.open("w") as stream:
        args = (["bazel", "build", "--aspects=//tools/clang_tidy:clang_tidy.bzl%clang_tidy", "--output_groups=default,clang_tidy", STUB]
                if focused else ["tools/lint.sh", "clang-tidy"])
        proc = subprocess.run(args, env=ENV, stdout=stream, stderr=subprocess.STDOUT, timeout=2600)
    row["entry_exit"] = proc.returncode
    info = subprocess.run([str(WORK / "bazel"), "info", "execution_root"], text=True, capture_output=True, timeout=90)
    root = Path(info.stdout.strip().splitlines()[-1]) if info.returncode == 0 else Path("bazel-out").resolve().parent
    collect(row, log, bep, executions, root)
    actions = row.get("actions", [])
    # The six hundred-plus source checks are architecture-sensitive. The exact
    # count follows the current workspace's full pattern, not an old inventory.
    prefix = "x86_64" if cpu == "amd64" else "aarch64"
    row["passed"] = (row.get("bazel_exit") == row["entry_exit"] == 0
                     and row.get("action_count", 0) >= (2 if focused else 300)
                     and row.get("verified_outputs") == row.get("fresh_scratch") == len(actions)
                     and all(a["program"].endswith("/clang-tidy") and a["program_sha256"] and a["exit"] == 0 for a in actions)
                     and any(prefix in flag for flag in row.get("target_flags",{})))
    row["assembly"] = [a["source"] for a in actions if a["source"].lower().endswith((".s", ".asm"))]
    row["stub_sources"] = [a["source"] for a in actions if str(PKG) in a["source"]]
    row["passed"] = row["passed"] and not row["assembly"] and len(row["stub_sources"]) == (2 if focused else 4)
    if focused:
        path = Path("bazel-bin") / PKG / "sighandler.built-in.bin.o"
        if path.is_file():
            row["elf"] = elf(path)
        linked = row.get("elf", {})
        row["passed"] = bool(row["passed"] and linked.get("machine") == (62 if cpu=="amd64" else 183) and linked.get("memcpy") and linked.get("undefined")==[])
    row["status"] = "PASS" if row["passed"] else "FAIL"
    save()
    executions.unlink(missing_ok=True)


def receiver(cpu, config):
    row = REPORT.setdefault("go_receiver", {})[cpu] = {}
    bazel = [str(WORK/"bazel"), "--bazelrc="+str(WORK/"connection.rc")]
    common = ["--config="+config, "-c", "opt", "--lockfile_mode=off"]
    prefix = "//" + str(PKG) + ":"
    labels = [prefix+"sysmsg",prefix+"sighandler_binary_arch"]
    log=WORK/(cpu+".receiver-log")
    with log.open("w") as stream:
        run=subprocess.run(bazel+["build",*common,"--spawn_strategy=local","--remote_download_outputs=toplevel",*labels],stdout=stream,stderr=subprocess.STDOUT,timeout=1100)
    row["build_exit"]=run.returncode
    if run.returncode:
        row["diagnostics"]=[s.strip()[:380] for s in log.read_text(errors="replace").splitlines() if s.startswith("ERROR:") or ": error:" in s][:12]
        save()
        return
    def outputs(label):
        c=subprocess.run(bazel+["cquery",*common,"--output=files",prefix+label],text=True,capture_output=True,timeout=180,check=True)
        return [Path(s.strip()) for s in c.stdout.splitlines() if s.startswith("bazel-out/")]
    chunks=[p for p in outputs("sighandler_binary_arch") if p.name=="sighandler.built-in."+cpu+".bin"]
    assert len(chunks)==1,chunks
    raw=chunks[0].read_bytes()
    row["raw"]={"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}
    row["library_outputs"]=[]
    for lib in outputs("sysmsg"):
        data=lib.read_bytes()
        row["library_outputs"].append({"path":str(lib),"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest(),"embedded_offset":data.find(raw)})
    row["pass"]=len(raw)>256 and any(p["embedded_offset"]>=0 for p in row["library_outputs"])
    save()

def main():
    save()
    configs=(("amd64","x86_64"),("arm64","aarch64"))
    for focused in (True,False):
        if not focused and any(not r.get("passed") for r in REPORT["architectures"].values()):
            break
        for cpu, config in configs:
            key=cpu+"-focused" if focused else cpu
            try:
                run(cpu, config, focused)
            except Exception as exc:
                REPORT["architectures"].setdefault(key,{})["error"] = (type(exc).__name__ + ": " + str(exc))[:550]
                save()
    passed = all(r.get("passed") for r in REPORT["architectures"].values())
    for cpu,config in configs:
        try:
            receiver(cpu,config)
        except Exception as exc:
            REPORT.setdefault("go_receiver",{}).setdefault(cpu,{})["error"]=(type(exc).__name__+": "+str(exc))[:300]
            save()
    passed = passed and all(r.get("pass") for r in REPORT["go_receiver"].values())
    REPORT["status"]="PASS" if passed else "FAIL"
    save()
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
