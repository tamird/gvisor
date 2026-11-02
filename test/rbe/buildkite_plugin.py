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
        for line in (err.read_text(errors="replace") + "\n" + out.read_text(errors="replace")).splitlines():
            if not re.search(r"ERROR:|error:|fatal:|not found|No such file|incompatible|requires|failed:", line, re.I):
                continue
            # Only text diagnostics; never publish action commands, environments, or signed URLs.
            if re.search(r"https?://|token|secret|credential|authorization|bearer |api.key|/bin/(?:ba)?sh -c|exec env", line, re.I):
                continue
            diagnostic.append(line[-650:])
    RESULT["phases"][name] = {"started": start, "finished": now(), "exit": proc.returncode, "diagnostics": diagnostic[-22:]}
    save()
    if require and proc.returncode:
        raise ValueError(f"{name} exited {proc.returncode}; see recorded diagnostics")
    return proc.returncode, out


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
    expr = f'filter("libpluginstack//:libpluginstack$", deps({TARGET}))'
    for name, flags in [("off", ["-c", "opt"]), ("on", command[2:-1])]:
        _, output = run("selection-" + name, [*BAZEL, "cquery", *flags, expr, "--output=label"], 480)
        labels = output.read_text().splitlines()
        RESULT[name + "_archive_labels"] = labels
        save()
        assert all(re.search(r"libpluginstack//:libpluginstack(?: |$)", label) for label in labels)
        assert bool(labels) == (name == "on")
    ctx = {"expected_source": SOURCE, "command_kind": "build"}
    def retain(key, data):
        ctx[key] = key + ".txt"
        (WORK / ctx[key]).write_bytes(data)
    retain("commit_file", git("cat-file", "commit", SOURCE))
    retain("source_before", git("status", "--porcelain", "--untracked-files=no"))
    raw, events = WORK / "original.bep", WORK / "events.jsonl"
    args = [*command[1:-1], f"--build_event_json_file={raw}", "--build_event_json_file_path_conversion=false", TARGET]
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
    link_expr = f'inputs("libpluginstack[.]a$", mnemonic("GoLink", {TARGET}))'
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
