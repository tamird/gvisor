"""Observe the released LuaJIT VM generator in the existing image build graph."""

import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
from datetime import datetime, timezone

TMP = Path(os.environ["RUNNER_TEMP"])
PRIVATE = TMP / "luajit-private"
PUBLIC = TMP / "luajit-public"
PRIVATE.mkdir(exist_ok=True)
PUBLIC.mkdir(exist_ok=True)
RC = TMP / "images.bazelrc"
BZ = ["bazel", "--bazelrc=" + str(RC)]
RESULT = {"source": os.environ["GITHUB_SHA"], "base": "5bb80cbb9c07ec3f597f3e5200102b6c8e0a95f4",
          "module": "luajit@2.1.0-beta3", "status": "INCOMPLETE", "arms": {}}


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save():
    data = json.dumps(RESULT, indent=2).encode() + b"\n"
    secrets = re.findall(r"(?:remote_header|bes_header)=x-buildbuddy-api-key=(\S+)", RC.read_text())
    assert secrets and all(secret.encode() not in data for secret in secrets)
    assert not re.search(rb"(?i)(authorization:|bearer |x-buildbuddy-api-key=|gh[pousr]_[A-Za-z0-9]{20})", data)
    (PUBLIC / "result.json").write_bytes(data)


def run(name, args, row):
    start = now()
    save()
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    (PRIVATE / (name + ".stdout")).write_text(p.stdout)
    (PRIVATE / (name + ".stderr")).write_text(p.stderr)
    lines = p.stderr.splitlines()
    selected = set()
    for i, line in enumerate(lines):
        if line.startswith(("ERROR:", "FAIL:")) or re.search(r"(?:Error:|error:|unrecognized instruction)", line):
            selected.update(range(max(0, i - 1), min(len(lines), i + 14)))
    diagnostic = [lines[i] for i in sorted(selected)[:90]
                  if not re.search(r"(?i)(authorization|remote_header|bes_header|x-buildbuddy|bearer)", lines[i])]
    entry = {"command": args, "exit": p.returncode, "started": start, "finished": now(),
             "stdout_sha256": sha(p.stdout.encode()), "stderr_sha256": sha(p.stderr.encode()),
             "invocations": sorted(set(re.findall(r"invocation/([0-9a-f-]{36})", p.stderr))),
             "diagnostics": diagnostic}
    row.setdefault("commands", []).append(entry)
    save()
    return p


def observe(cpu):
    row = {"status": "INCOMPLETE"}
    RESULT["arms"][cpu] = row
    try:
        bep = PRIVATE / (cpu + ".bep.json")
        p = run(cpu + "-build", BZ + ["build", "--config=" + cpu,
                "--build_event_json_file=" + str(bep), "@luajit//:luajit", "@luajit//:bin/luajit"], row)
        row["build_exit"] = p.returncode
        row["build_finished"] = now()
        if bep.exists():
            events = [json.loads(line) for line in bep.read_text().splitlines()]
            row["bep_sha256"] = sha(bep.read_bytes())
            row["targets"] = [{"id": e["id"]["targetCompleted"], "success": e["completed"].get("success", False)}
                              for e in events if "targetCompleted" in e.get("id", {}) and "completed" in e]
        q = run(cpu + "-actions", BZ + ["aquery", "--config=" + cpu, "--output=jsonproto",
                'mnemonic("Genrule|CppCompile|CppLink", deps(@luajit//:bin/luajit))'], row)
        assert q.returncode == 0, "Action inventory unavailable"
        graph = json.loads(q.stdout)
        labels = {v["id"]: v["label"] for v in graph["targets"]}
        row["actions"] = [{"owner": labels[a["targetId"]], "configuration": a.get("configurationId"),
                            "mnemonic": a["mnemonic"], "arguments": a.get("arguments", [])}
                           for a in graph["actions"] if "luajit+//:" in labels[a["targetId"]]
                           and (a["mnemonic"] == "Genrule" or any(
                               token in " ".join(a.get("arguments", []))
                               for token in ("src/host/buildvm.c", "src/lj_vm.S")))]
        assert row["actions"] and any(a["mnemonic"] == "Genrule" for a in row["actions"])
        if p.returncode == 0:
            q = run(cpu + "-files", BZ + ["cquery", "--config=" + cpu, "--output=files", "@luajit//:bin/luajit"], row)
            assert q.returncode == 0
            files = q.stdout.splitlines()
            assert len(files) == 1, files
            data = Path(files[0]).read_bytes()
            assert data[:6] == b"\x7fELF\x02\x01"
            machine = struct.unpack_from("<H", data, 18)[0]
            assert machine == {"x86_64": 62, "aarch64": 183}[cpu]
            row["elf"] = {"path": files[0], "sha256": sha(data), "size": len(data), "machine": machine}
            if cpu == "x86_64":
                v = run(cpu + "-version", [files[0], "-v"], row)
                assert v.returncode == 0 and "LuaJIT 2.1.0-beta3" in v.stdout + v.stderr
                row["version"] = (v.stdout + v.stderr).strip()
        row["status"] = "OBSERVED"
    except Exception as error:
        row["error"] = f"{type(error).__name__}: {error}"[:2000]
    finally:
        save()


RESULT["started"] = now()
assert subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip() == RESULT["base"]
assert not subprocess.check_output(["git", "diff", "HEAD", "--"], text=True)
RESULT["module_sha256"] = sha(Path("MODULE.bazel").read_bytes())
for cpu in ("x86_64", "aarch64"):
    observe(cpu)
try:
    row = RESULT["supplier"] = {}
    p = run("output-base", BZ + ["info", "output_base"], row)
    assert p.returncode == 0
    root = Path(p.stdout.strip()) / "external/luajit+"
    row["source_sha256"] = {name: sha((root / name).read_bytes()) for name in ("BUILD.bazel", "src/lj_arch.h", "COPYRIGHT")}
    for name in ("COPYRIGHT", "src/jit/bcsave.lua"):
        p = run("public-" + name.replace("/", "-"), BZ + ["build", "@luajit//:" + name], row)
        row.setdefault("public_inputs", {})[name] = p.returncode
    row["status"] = "OBSERVED"
except Exception as error:
    RESULT["supplier"]["error"] = f"{type(error).__name__}: {error}"[:2000]
RESULT["status"] = "OBSERVED" if all(row["status"] == "OBSERVED" for row in RESULT["arms"].values()) and RESULT["supplier"].get("status") == "OBSERVED" else "INCOMPLETE"
RESULT["finished"] = now()
save()
print(json.dumps({"status": RESULT["status"], "arms": {k: {"status": v["status"], "build_exit": v.get("build_exit")} for k, v in RESULT["arms"].items()}}))
raise SystemExit(RESULT["status"] != "OBSERVED")
