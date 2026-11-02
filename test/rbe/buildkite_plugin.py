"""Observe the native Buildkite GNU plugin target; keep only the typed receipt."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import time

import qualification_report as report


WORK = Path(os.environ["RUNNER_TEMP"]) / "plugin-native"
SOURCE = os.environ["SOURCE"]
TARGET = "//runsc:runsc-plugin-stack"
BAZEL = ["bazel"]
START = time.monotonic()
DEADLINE = START + 48 * 60
RESULT = {"source": SOURCE, "target": TARGET, "status": "INCOMPLETE", "phases": {}}


def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    (WORK / "result.json").write_text(json.dumps(RESULT, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args])


def run(name, argv, limit, *, require=True):
    limit = max(1, min(limit, int(DEADLINE - time.monotonic())))
    prefix = WORK / name
    out, err = prefix.with_suffix(".out"), prefix.with_suffix(".err")
    start = now()
    with out.open("wb") as stdout, err.open("wb") as stderr:
        proc = subprocess.run(["timeout", "--kill-after=15s", str(limit), *argv], stdout=stdout, stderr=stderr)
    diagnostic = []
    if proc.returncode:
        log = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", err.read_text(errors="replace") + "\n" + out.read_text(errors="replace")).splitlines()
        keep = {offset for i, line in enumerate(log)
                if re.search(r"error:|fatal:|not found|No such file|incompatible|requires|failed:|fork/exec|execve", line, re.I)
                for offset in (i-2, i-1, i, i+1) if 0 <= offset < len(log)}
        for i in sorted(keep):
            line = log[i].strip()
            if not line or re.search(r"https?://|token|secret|credential|authorization|bearer |api.key|/bin/(?:ba)?sh -c|exec env", line, re.I):
                continue
            if line.startswith(("PATH=", "HOME=", "GOPATH=", "GOCACHE=")):
                continue
            diagnostic.append(line[:1000])
    RESULT["phases"][name] = {"started": start, "finished": now(), "exit": proc.returncode, "diagnostics": diagnostic[-22:]}
    save()
    if require and proc.returncode:
        raise ValueError(f"{name} exited {proc.returncode}; see recorded diagnostics")
    return proc.returncode, out


def compilers(name, flags):
    expr = f'mnemonic("GoStdlib", deps({TARGET}))'
    _, out = run("actions-" + name, [*BAZEL, "aquery", *flags, expr, "--output=jsonproto", "--noinclude_artifacts"], 480)
    graph = json.loads(out.read_bytes())
    targets = {t["id"]: t["label"] for t in graph.get("targets", [])}
    seen = {}
    rows = []
    RESULT[name + "_stdlib"] = rows
    RESULT[name + "_action_count"] = len(graph.get("actions", []))
    save()
    for action in graph.get("actions", []):
        # Protobuf JSON omits fields with default scalar values, e.g. CC="".
        env = {e.get("key", ""): e.get("value", "") for e in action.get("environmentVariables", [])}
        args = action.get("arguments", [])
        row = {"config": action.get("configurationId"), "owner": targets.get(action.get("targetId"), ""),
               "arch": env.get("GOARCH"), "os": env.get("GOOS"), "CC": env.get("CC"),
               "cgo": env.get("CGO_ENABLED"), "action_key": action.get("actionKey")}
        for flag in ("-goos", "-goarch", "-out", "-sdk"):
            if flag in args:
                row[flag] = args[args.index(flag) + 1]
        cc = row["CC"]
        if cc and cc.startswith("/usr/bin/") and re.fullmatch(r"[A-Za-z0-9./_+\-]+", cc):
            if cc not in seen:
                try:
                    run_cc = subprocess.run([cc, "--version"], capture_output=True, timeout=10)
                    machine = subprocess.run([cc, "-dumpmachine"], capture_output=True, timeout=10)
                    seen[cc] = {"exit": run_cc.returncode, "stdout": run_cc.stdout.decode().splitlines()[:1],
                                "machine": machine.stdout.decode().strip(), "machine_exit": machine.returncode, "realpath": os.path.realpath(cc)}
                except FileNotFoundError as exc:
                    seen[cc] = {"errno": exc.errno, "filename": exc.filename}
            row["invocation"] = seen[cc]
        rows.append(row)
        save()
    assert any(r["owner"].endswith("//:stdlib") for r in rows), "aquery returned no GoStdlib for the declared owner"
    return rows


def main():
    assert git("rev-parse", "HEAD").decode().strip() == SOURCE
    assert not git("status", "--porcelain", "--untracked-files=no")
    assert os.environ["DOCKER_BUILD"] == "false" and not Path(os.environ["DOCKER_HOST"].removeprefix("unix://")).exists()
    pipeline = Path(".buildkite/pipeline.yaml").read_text()
    lines = pipeline.split('label: ":speedboat: Compile runsc-plugin-stack (AMD64)"\n')
    assert len(lines) == 2
    block = lines[1].split("\n\n", 1)[0]
    assert 'DOCKER_BUILD: "false"' in block and 'arch: "amd64"' in block and 'queue: "default"' in block
    command = shlex.split(re.search(r"^    command: (.+)$", block, re.M)[1])
    assert command == ["bazel", "build", "-c", "opt", "--config=plugin-tldk", TARGET]
    RESULT.update(command=command, machine=os.uname().machine, docker_daemon=False,
                  declared_plugin_source="675d49b1c7a1b43ac0d2a5cbe4ab5d897ff44afa",
                  native_tools={name: shutil.which(name) for name in ("gcc", "aarch64-linux-gnu-gcc", "make", "patch", "pkg-config")},
                  free_bytes=shutil.disk_usage(".").free)
    _, version = run("bazel-version", [*BAZEL, "--version"], 180)
    RESULT["bazel_version"] = version.read_text().strip()
    assert RESULT["bazel_version"] == "bazel " + Path(".bazelversion").read_text().strip()
    RESULT["original"] = {"run": 41394053559, "artifact": 13118243934,
                          "zip_sha256": "99de4d781ea3478b44cac2eaac3d3aaede49d183713bd0ea9bd7bf944810a6a7",
                          "native_exit": 1, "selection_reused": {"off": [], "on": ["@libpluginstack//:libpluginstack (6a9c73a)"]}}
    rows = compilers("stock", command[2:-1])
    # Only replace an ARM compiler that this actual host just tried to exec
    # and could not find. Retain Coral's normal AMD64 GNU link toolchain.
    def cpu(row):
        return row["arch"] or row.get("-goarch")
    missing = {(cpu(r), r["CC"]) for r in rows if r.get("invocation", {}).get("errno") == 2}
    if missing == {("arm64", "/usr/bin/aarch64-linux-gnu-gcc")} and any(
            cpu(r) == "amd64" and r.get("invocation", {}).get("exit") == 0 and r["invocation"]["machine_exit"] == 0
            and r["invocation"]["machine"] == "x86_64-linux-gnu" for r in rows):
        override = "--extra_toolchains=@llvm//toolchain:linux_x86_64_to_linux_aarch64"
        command.insert(-1, override)
        fixed = compilers("arm-override", command[2:-1])
        assert {(cpu(r), r["CC"]) for r in rows if cpu(r) != "arm64"} == {
            (cpu(r), r["CC"]) for r in fixed if cpu(r) != "arm64"}
        assert all((cpu(r), r["CC"]) not in missing for r in fixed)
        assert any(cpu(r) == "arm64" and "llvm" in (r["CC"] or "") for r in fixed)
        RESULT["mode"] = "declared ARM compiler; original GNU AMD64 product"
    else:
        # The first failure cannot be attributed to an absent ARM compiler.
        # Retain verbose stderr from the actual target before changing it.
        RESULT["mode"] = "reproduce original configuration with concrete action diagnostics"
    RESULT["effective_command"] = command.copy()
    save()
    ctx = {"expected_source": SOURCE, "command_kind": "build"}
    def retain(key, data):
        ctx[key] = key + ".txt"
        (WORK / ctx[key]).write_bytes(data)
    retain("commit_file", git("cat-file", "commit", SOURCE))
    retain("source_before", git("status", "--porcelain", "--untracked-files=no"))
    raw, events = WORK / "original.bep", WORK / "events.jsonl"
    args = [*command[1:-1], f"--build_event_json_file={raw}", "--build_event_json_file_path_conversion=false", "--verbose_failures", "--color=no", "--curses=no", TARGET]
    retain("executed_arguments", ("\n".join(args) + "\n").encode())
    rc, _ = run("native-build", [*BAZEL, *args], 45 * 60, require=False)
    ctx["execution_finished_at"] = RESULT["phases"]["native-build"]["finished"]
    retain("native_exit", str(rc).encode())
    retain("final_head", git("rev-parse", "HEAD"))
    retain("source_after", git("status", "--porcelain", "--untracked-files=no"))
    capture = report.capture(raw, events, Path("bazel-out"), WORK / "reports")
    retain("capture_exit", str(int(bool(capture["errors"]))).encode())
    context = WORK / "context.json"
    context.write_text(json.dumps(ctx))
    targets = WORK / "targets.txt"
    targets.write_text(TARGET + "\n")
    verdict = report.verdict(events, report.manifest_path(events), context, expected_targets=targets)
    RESULT["build_verdict"] = {k: verdict[k] for k in ("source", "status", "native_exit", "errors", "failures", "expected", "missing", "execution_finished_at", "expected_labels")}
    save()
    assert verdict["status"] == "PASS" and verdict["expected_labels"] == [TARGET]
    _, files = run("product", [*BAZEL, "cquery", *command[2:-1], TARGET, "--output=files"], 90)
    relative, = files.read_text().splitlines()
    elf = Path(relative)
    data = elf.read_bytes()
    _, headers = run("elf", ["readelf", "-hlWd", str(elf)], 45)
    desc = headers.read_text()
    assert data[:4] == b"\x7fELF" and "Advanced Micro Devices X86-64" in desc
    assert not re.search(r"^\s*INTERP\s|\(NEEDED\)", desc, re.M)
    _, stdout = run("version", [str(elf.resolve()), "--version"], 45)
    version_text = stdout.read_text().strip()
    assert version_text.startswith("runsc version ") and "\nspec: " in version_text
    RESULT["product"] = {"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                         "machine": "X86_64", "PT_INTERP": False, "DT_NEEDED": [], "version": version_text}
    # Go flags can strip symbol tables, so retain the direct GoLink input
    # independently of the optional identity-by-name in the finished ELF.
    link_expr = f'inputs(".*/libpluginstack[.]a$", mnemonic("GoLink", {TARGET}))'
    _, graph = run("link-input", [*BAZEL, "aquery", *command[2:-1], link_expr, "--output=jsonproto",
                                  "--noinclude_artifacts", "--noinclude_commandline"], 90)
    link = json.loads(graph.read_bytes())
    owners = {t["id"]: t["label"] for t in link.get("targets", [])}
    RESULT["plugin_link_owners"] = [owners[a["targetId"]] for a in link.get("actions", []) if a["mnemonic"] == "GoLink"]
    _, symbols = run("symbols", ["nm", "--defined-only", str(elf)], 45, require=False)
    RESULT["defined_plugin_preinitstack"] = any(line.endswith(" T plugin_preinitstack") for line in symbols.read_text().splitlines())
    assert TARGET in RESULT["plugin_link_owners"] or RESULT["defined_plugin_preinitstack"]
    RESULT["status"] = "PASS"


if __name__ == "__main__":
    save()
    try:
        main()
    except Exception as exc:
        RESULT["error"] = f"{type(exc).__name__}: {exc}"
    RESULT["decided"] = now()
    save()
    raise SystemExit(RESULT["status"] != "PASS")
