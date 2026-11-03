"""Check the proc_net include repair with its actual compiler and pinned linter."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

SOURCE = "26d73328a391591edbd8a91e073a5564b6010ec3"
TARGET = "//test/syscalls/linux:proc_net_test"
FILE = Path("test/syscalls/linux/proc_net.cc")
ROOT = Path(os.environ["RUNNER_TEMP"])
REPORT = ROOT / "proc-net-review.json"
OUT = {"source": SOURCE, "target": TARGET, "verdict": "INCOMPLETE", "phases": {}}


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def save():
    REPORT.write_text(json.dumps(OUT, indent=2, sort_keys=True) + "\n")


def run(args, row):
    row["started"] = utc()
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    row.update(exit=proc.returncode, finished=utc(), output_sha256=digest(proc.stdout))
    row["diagnostics"] = [line[:500] for line in proc.stdout.decode(errors="replace").splitlines()
                          if (line.startswith("ERROR:") or "[misc-include-cleaner]" in line or "sources analyzed" in line)
                          and not any(word in line.lower() for word in ("token", "credential", "api-key", "authorization"))][:30]
    save()
    return proc


def rows(path):
    data = path.read_text()
    decoder = json.JSONDecoder()
    offset = 0
    while offset < len(data):
        while offset < len(data) and data[offset].isspace():
            offset += 1
        if offset == len(data):
            return
        value, offset = decoder.raw_decode(data, offset)
        yield value


def phase(name, fn):
    row = OUT["phases"][name] = {"verdict": "INCOMPLETE"}
    try:
        fn(row)
        row["verdict"] = "PASS"
    except Exception as exc:
        row.update(verdict="FAIL", error=str(exc))
    finally:
        save()


def build(row):
    require(subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip() == SOURCE, "wrong source")
    before = FILE.read_bytes()
    require(before.count(b'#include "test/util/logging.h"') == 1, "direct logging include absent")
    row["source_sha256"] = digest(before)
    row["invocation"] = str(uuid.uuid4())
    bep = ROOT / "proc-net.bep.json"
    spawns = ROOT / "proc-net.exec.json"
    command = ["bazel", "--bazelrc=" + os.environ["QA_RC"], "build", "--config=x86_64", "--jobs=16",
               "--aspects=//tools:cc_compile_inputs.bzl%cc_compile_inputs", "--output_groups=default,cc_compile_inputs",
               "--invocation_id=" + row["invocation"], "--build_event_json_file=" + str(bep),
               "--execution_log_json_file=" + str(spawns), TARGET]
    proc = run(command, row)
    completed = [event["completed"].get("success", False) for event in rows(bep)
                 if event.get("id", {}).get("targetCompleted", {}).get("label") == TARGET
                 and not event["id"]["targetCompleted"].get("aspect")]
    row["completed"] = completed
    require(proc.returncode == 0 and completed == [True], "original build did not complete")
    selected = [spawn for spawn in rows(spawns) if spawn.get("mnemonic") == "CppCompile"
                and spawn.get("targetLabel") == TARGET and str(FILE) in spawn.get("commandArgs", [])]
    require(len(selected) == 1, "missing or ambiguous proc_net compiler")
    spawn, = selected
    files = {item["path"]: item.get("digest", {}) for item in spawn.get("inputs", [])}
    require(files[str(FILE)]["hash"] == digest(before), "compiler source digest mismatch")
    require("test/util/logging.h" in files, "compiler lacks logging header")
    row["compile"] = {"runner": spawn["runner"], "cached": spawn.get("cacheHit", False),
                      "exit": spawn.get("exitCode", 0), "source": files[str(FILE)],
                      "header": files["test/util/logging.h"], "outputs": spawn.get("actualOutputs", []),
                      "platform": spawn.get("platform", {})}
    require(row["compile"]["exit"] == 0, "compile failed")
    execroot = subprocess.check_output(["bazel", "--bazelrc=" + os.environ["QA_RC"], "info", "execution_root"]).decode().strip()
    Path("compile_commands.json").write_text(json.dumps([{"file": str(FILE.resolve()), "directory": execroot,
                                                         "arguments": spawn["commandArgs"]}]))


def lint(row, missing):
    require(OUT["phases"]["build"]["verdict"] == "PASS", "compiler database unavailable")
    original = FILE.read_bytes()
    try:
        if missing:
            FILE.write_bytes(original.replace(b'#include "test/util/logging.h"\n', b""))
        row["source_sha256"] = digest(FILE.read_bytes())
        proc = run(["bash", "tools/lint.sh", "clang-tidy"], row)
        if missing:
            require(proc.returncode == 1 and any("TEST_PCHECK" in line and "[misc-include-cleaner]" in line
                                                for line in row["diagnostics"]), "missing-include control not observed")
        else:
            require(proc.returncode == 0 and any("1 sources analyzed" in line for line in row["diagnostics"]),
                    "repaired source did not pass pinned lint")
    finally:
        FILE.write_bytes(original)


OUT["started"] = utc()
phase("build", build)
phase("missing_include", lambda row: lint(row, True))
phase("direct_include", lambda row: lint(row, False))
OUT["verdict"] = "PASS" if all(row["verdict"] == "PASS" for row in OUT["phases"].values()) else "FAIL"
OUT["finished"] = utc()
save()
raise SystemExit(OUT["verdict"] != "PASS")
