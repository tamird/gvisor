"""Qualify one original Go SDK builder per native executor on pinned minimal OCI images."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid


SOURCE = "af5fe68ff1948589e6dbed1caeac40ad8daddbcf"
WORK = Path(os.environ["RUNNER_TEMP"]) / "sdk-native"
ARCHES = {
    "amd64": ("x86_64", "k8", 62, "d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"),
    "arm64": ("aarch64", "aarch64", 183, "b0aca6ddfad947e66782a29534647936d4d645a1b96d9c8a97e4f53b1607d428"),
}
WRAPPER = "1e0009c3cac0749da5d04d8652d0b9df95e474bdc11cc8b427bf188e26053bc2"
MNEMONIC = "GoToolchainBinaryBuild"
OUTPUTS = {"builder", "pack.exe", "covdata.exe"}
BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc"]
HEX = re.compile(r"[0-9a-f]{64}")
HINTS = {
    "missing-file": r"no such file or directory|file not found",
    "missing-interpreter": r"(?:interpreter|/usr/bin/env|/bin/(?:ba)?sh)[^\n]{0,120}(?:not found|no such file|does not exist)",
    "exec-start": r"fork/exec|execve|failed to (?:start|exec)|unable to (?:start|exec)|error running command",
    "exec-format": r"exec format error",
    "permissions": r"permission denied|operation not permitted",
    "capacity": r"resource.exhausted|no registered executors|deadline exceeded",
    "missing-sdk-target": r"no such (?:target|package)|repository .* could not be resolved",
}


def owner(arch):
    return "@@rules_go++go_sdk+main___download_0_linux_" + arch + "//:builder"


def platform(arch):
    return "//test/rbe/release_prewarmer:" + arch


def expected(arch):
    return {"Arch": arch, "OSFamily": "linux", "workload-isolation-type": "oci", "recycle-runner": "false",
            "container-image": "docker://gcr.io/distroless/static-debian13@sha256:" + ARCHES[arch][3]}


def flags(arch):
    return ["--config=" + ARCHES[arch][0], "-c", "opt", "--jobs=1", "--lockfile_mode=off",
            "--host_platform=" + platform(arch), "--extra_execution_platforms=" + platform(arch),
            "--noremote_accept_cached", "--disk_cache=", "--noremote_local_fallback",
            "--modify_execution_info=^GoToolchainBinaryBuild$=+no-remote-cache",
            "--color=no", "--curses=no", "--noshow_progress"]


def save(arch, row):
    raw = json.dumps(row, indent=2) + "\n"
    assert len(raw) < 48000 and not re.search(r"(?i)authorization\s*[:=]|x-buildbuddy-api-key|github_pat_|gh[pousr]_", raw)
    (WORK / "results" / (arch + ".json")).write_text(raw)


def signals(text):
    return [name for name, pattern in HINTS.items() if re.search(pattern, text, re.I)]


def missing_executable(text):
    for line in text.splitlines():
        if not re.search(r"no such file or directory|not found|does not exist", line, re.I):
            continue
        for path in ("/usr/bin/env", "/bin/bash", "/bin/sh", "/go/private/rules/binary_wrapper.sh"):
            if path in line:
                return path
    return None


def digest(item):
    if item and HEX.fullmatch(item.get("hash", "")) and str(item.get("sizeBytes", "")).isdigit():
        return {"hash": item["hash"], "bytes": int(item["sizeBytes"])}
    return None


def analyze(arch, row):
    sdk = owner(arch)
    path = WORK / (arch + ".aquery")
    log = WORK / (arch + ".analysis.log")
    with path.open("wb") as out, log.open("wb") as err:
        rc = subprocess.run([*BAZEL, "aquery", *flags(arch), "--remote_executor=", "--remote_cache=", "--bes_backend=",
                             "--output=jsonproto", "--include_commandline", "--noinclude_artifacts", '"' + sdk + '"'],
                            stdout=out, stderr=err, timeout=470).returncode
    graph = row["graph"] = {"exit": rc, "signals": signals(log.read_text(errors="replace")), "eligible": False}
    if rc:
        return
    data = json.loads(path.read_text())
    labels = {str(t["id"]): t["label"] for t in data.get("targets", [])}
    configs = {str(c["id"]): c for c in data.get("configuration", [])}
    actions = data.get("actions", [])
    graph["mnemonics"] = dict(Counter(a["mnemonic"] for a in actions))
    group = [a for a in actions if a.get("mnemonic") == MNEMONIC]
    if len(group) != 1:
        return
    action, = group
    label = labels[str(action["targetId"])]
    cfg = configs[str(action["configurationId"])]
    exe = action["arguments"][0]
    props = {p["key"]: p.get("value", "") for p in action.get("executionInfo", [])}
    graph.update(owner=label, configuration=cfg.get("checksum"), cpu=cfg.get("platformName"),
                 action_key=action.get("actionKey"), platform=action.get("executionPlatform", ""),
                 worker={k: props.get(k) for k in expected(arch)}, wrapper=exe.endswith("/go/private/rules/binary_wrapper.sh"),
                 declared_outputs=[Path(arg).name for arg in action["arguments"][1:4]])
    base = subprocess.check_output([*BAZEL, "info", "--remote_executor=", "--remote_cache=", "--bes_backend=",
                                    "--lockfile_mode=off", "output_base"], stderr=subprocess.DEVNULL, text=True).strip()
    if exe.startswith("external/") and ".." not in Path(exe).parts:
        candidate = Path(base) / exe
        if candidate.is_file() and candidate.stat().st_size < 100000:
            content = candidate.read_bytes()
            graph["stock_wrapper_sha256"] = hashlib.sha256(content).hexdigest()
            graph["env_bash_shebang"] = content.splitlines()[0] == b"#!/usr/bin/env bash"
    graph["eligible"] = (label.lstrip("@") == sdk.lstrip("@") and graph["platform"].lstrip("@") == platform(arch)
                         and graph["cpu"] == ARCHES[arch][1] and graph["worker"] == expected(arch)
                         and graph["wrapper"] and graph.get("stock_wrapper_sha256") == WRAPPER
                         and graph.get("env_bash_shebang") and set(graph["declared_outputs"]) == OUTPUTS)


def spawns(path):
    if not path.is_file():
        return []
    assert path.stat().st_size <= 128 << 20
    raw = path.read_text()
    decoder = json.JSONDecoder()
    rows = []
    offset = 0
    while offset < len(raw):
        while offset < len(raw) and raw[offset].isspace():
            offset += 1
        if offset == len(raw):
            break
        value, offset = decoder.raw_decode(raw, offset)
        rows.extend(value if isinstance(value, list) else [value])
    return rows


def original(arch, row):
    sdk = owner(arch)
    mark = str(uuid.uuid4())
    bep, log, execution = (WORK / (arch + ext) for ext in (".bep", ".physical.log", ".spawns"))
    with log.open("wb") as out:
        try:
            rc = subprocess.run([*BAZEL, "--bazelrc=" + str(WORK / "remote.bazelrc"), "build", *flags(arch),
                                 "--spawn_strategy=remote", "--remote_download_outputs=toplevel", "--remote_timeout=240",
                                 "--build_event_publish_all_actions", "--invocation_id=" + mark,
                                 "--build_event_json_file=" + str(bep), "--execution_log_json_file=" + str(execution), sdk],
                                stdout=out, stderr=out, timeout=430).returncode
        except subprocess.TimeoutExpired:
            rc = 124
    console = log.read_text(errors="replace")
    result = row["physical"] = {"invocation": mark, "exit": rc, "signals": signals(console), "enoent_hint": missing_executable(console),
                                "events": [], "spawns": [], "outcome": "INCOMPLETE"}
    started, finished = [], []
    if bep.exists():
        for line in bep.open():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                result["truncated_bep"] = True
                break
            if start := event.get("started"):
                started.append(start.get("uuid"))
            if done := event.get("finished"):
                finished.append(done.get("exitCode", {}).get("code", 0))
            action = event.get("action", {})
            ident = event["id"].get("actionCompleted", {})
            if ident.get("label", "").lstrip("@") == sdk.lstrip("@") and action.get("type") == MNEMONIC:
                details = action.get("failureDetail", {})
                codes = {k: v["code"] for k, v in details.items() if isinstance(v, dict) and
                         re.fullmatch(r"[A-Z_0-9]{1,90}", str(v.get("code", "")))}
                result["events"].append({"success": bool(action.get("success")), "exit": action.get("exitCode", 0),
                                         "configuration": ident.get("configuration", {}).get("id"),
                                         "failure_codes": codes, "signals": signals(details.get("message", "")),
                                         "enoent_hint": missing_executable(details.get("message", ""))})
    all_spawns = spawns(execution)
    result["spawn_counts"] = dict(Counter(s.get("mnemonic", "unknown") for s in all_spawns))
    for spawn in all_spawns:
        if spawn.get("targetLabel", "").lstrip("@") != sdk.lstrip("@") or spawn.get("mnemonic") != MNEMONIC:
            continue
        props = {p["name"]: p["value"] for p in spawn.get("platform", {}).get("properties", [])}
        args = spawn.get("commandArgs", [])
        wrappers = [digest(f.get("digest")) for f in spawn.get("inputs", []) if f["path"].endswith("/go/private/rules/binary_wrapper.sh")]
        outputs = {Path(f["path"]).name: digest(f.get("digest")) for f in spawn.get("actualOutputs", [])
                   if Path(f["path"]).name in OUTPUTS}
        status = spawn.get("status", "")
        result["spawns"].append({"runner": spawn.get("runner"), "cached": bool(spawn.get("cacheHit")),
                                 "exit": int(spawn.get("exitCode", 0)), "has_error_status": bool(status),
                                 "signals": signals(status), "enoent_hint": missing_executable(status),
                                 "worker": {k: props.get(k) for k in expected(arch)},
                                 "digest": digest(spawn.get("digest")), "wrapper_input": any(w and w["hash"] == WRAPPER for w in wrappers),
                                 "executes_stock_wrapper": bool(args and args[0].endswith("/go/private/rules/binary_wrapper.sh")),
                                 "outputs": outputs})
    if started != [mark] or finished != [rc] or result.get("truncated_bep") or len(result["events"]) != 1 or len(all_spawns) != 1 or len(result["spawns"]) != 1:
        return
    action, = result["spawns"]
    event, = result["events"]
    if (action["runner"] != "remote" or action["cached"] or action["worker"] != expected(arch)
            or not action["executes_stock_wrapper"] or not action["wrapper_input"]):
        return
    if rc or not event["success"] or action["exit"] or action["has_error_status"]:
        result["outcome"] = "FRESH_REMOTE_FAILURE"
        return
    if set(action["outputs"]) != OUTPUTS or not all(action["outputs"].values()):
        return
    label_repo = sdk.lstrip("@").split("//")[0]
    binary = Path("bazel-bin/external") / label_repo / "builder"
    if binary.is_file():
        with binary.open("rb") as stream:
            header = stream.read(64)
            stream.seek(0)
            hashed = hashlib.file_digest(stream, "sha256").hexdigest()
        result["builder_elf"] = {"machine": int.from_bytes(header[18:20], "little"), "elf64le": header[:6] == b"\x7fELF\x02\x01",
                                 "bytes": binary.stat().st_size, "digest_matches": hashed == action["outputs"]["builder"]["hash"]}
        elf = result["builder_elf"]
        if elf["elf64le"] and elf["machine"] == ARCHES[arch][2] and elf["digest_matches"]:
            result["outcome"] = "FRESH_REMOTE_PASS"


def main():
    phase, = sys.argv[1:]
    assert phase in ("graph", "build")
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    outcomes = []
    for arch in ARCHES:
        receipt = WORK / "results" / (arch + ".json")
        row = json.loads(receipt.read_text()) if receipt.is_file() else {"source": SOURCE, "native_executor": arch,
                                                                         "sdk_owner": owner(arch), "image": expected(arch)}
        try:
            if phase == "graph":
                analyze(arch, row)
            elif row.get("graph", {}).get("eligible"):
                original(arch, row)
        except (OSError, ValueError, KeyError, IndexError, AssertionError, subprocess.SubprocessError) as exc:
            row[phase + "_observer_error"] = type(exc).__name__
        save(arch, row)
        passed = row.get("graph", {}).get("eligible") if phase == "graph" else row.get("physical", {}).get("outcome") == "FRESH_REMOTE_PASS"
        outcomes.append(bool(passed))
        print(arch, phase, row.get("physical", {}).get("outcome", passed), flush=True)
    return any(outcomes) if phase == "graph" else all(outcomes)


if __name__ == "__main__":
    raise SystemExit(not main())
