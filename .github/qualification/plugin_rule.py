"""Check the original package failure before building both corrected plugin binaries."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

import qualification_report as report


WORK = Path(os.environ["RUNNER_TEMP"]) / "plugin"
STOCK, FIXED = os.environ["STOCK"], os.environ["FIXED"]
LIB = "@libpluginstack//:libpluginstack"
TARGETS = ["//runsc:runsc-plugin-stack", "//runsc/cmd/sentry:gvisor_sentry_plugin_stack"]
CONFIG = ["-c", "opt", "--config=plugin-tldk", "--lockfile_mode=off"]
BAZEL = ["docker", "exec", "plugin", str(WORK / "bazel")]
DEADLINE = time.monotonic() + 44 * 60
RESULT = {"status": "INCOMPLETE", "source": FIXED, "stock": STOCK, "product": os.environ["PRODUCT"],
          "carrier": os.environ["GITHUB_SHA"], "bazel": "9.3.1", "architecture": "amd64", "phases": {},
          "expected_build": TARGETS, "runtime_tests_executed": 0}


def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    (WORK / "result.json").write_text(json.dumps(RESULT, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args])


def command(name, args, limit):
    RESULT["phase"] = name
    save()
    log = WORK / (name + ".log")
    start = now()
    with log.open("wb") as output:
        rc = subprocess.run(["timeout", "--kill-after=10", str(max(1, min(limit, int(DEADLINE - time.monotonic())))),
                             *BAZEL, *args], stdout=output, stderr=subprocess.STDOUT).returncode
    data = log.read_bytes()
    text = data.decode(errors="replace")
    diagnostic = [s[:550] for s in text.splitlines()
                  if ("ERROR:" in s or "fatal error:" in s or "Error in fail:" in s)
                  and not re.search(r"https?://|authorization|token|secret|api.key|credential|bearer|gh[pousr]_", s, re.I)]
    RESULT["phases"][name] = {"exit": rc, "started": start, "finished": now(),
                              "log_sha256": hashlib.sha256(data).hexdigest(), "diagnostics": diagnostic[:6]}
    save()
    print(name, rc, flush=True)
    return rc, text


def main():
    save()
    path = "external/tools/plugin-stack/plugin-stack.BUILD"
    git("checkout", "--detach", STOCK)
    assert git("rev-parse", "HEAD").decode().strip() == STOCK
    rc, stock = command("stock-rule", ["query", "--lockfile_mode=off", LIB], 360)
    assert rc and "This rule has been removed from Bazel" in stock and "cc_library(" in stock
    assert "new_local_repository+libpluginstack/BUILD.bazel" in stock
    git("checkout", "--detach", FIXED)
    assert git("rev-parse", "HEAD").decode().strip() == FIXED
    RESULT["overlay"] = git("rev-parse", f"HEAD:{path}").decode().strip()
    rc, fixed = command("corrected-rule", ["query", "--lockfile_mode=off", LIB], 360)
    assert rc == 0 and any(line.strip().endswith("libpluginstack//:libpluginstack") for line in fixed.splitlines())
    for target in TARGETS:
        name = "configured-" + target.split(":")[-1]
        rc, output = command(name, ["cquery", *CONFIG, f"somepath({target}, {LIB})"], 540)
        assert rc == 0 and any(line.startswith(target + " ") for line in output.splitlines())
        assert re.search(r"[@+\w-]*libpluginstack//:libpluginstack \(", output)
        RESULT["phases"][name]["dependency"] = LIB
        save()
    raw, events = WORK / "build.bep", WORK / "events.jsonl"
    args = ["build", *CONFIG, "--jobs=4", f"--build_event_json_file={raw}",
            "--build_event_json_file_path_conversion=false", *TARGETS]
    context = {"expected_source": FIXED, "command_kind": "build"}

    def retain(key, data):
        context[key] = key + ".txt"
        (WORK / context[key]).write_bytes(data)

    retain("commit_file", git("cat-file", "commit", FIXED))
    retain("source_before", git("status", "--porcelain", "--untracked-files=no"))
    retain("executed_arguments", ("\n".join(args) + "\n").encode())
    rc, _ = command("build", args, 32 * 60)
    context["execution_finished_at"] = RESULT["phases"]["build"]["finished"]
    retain("native_exit", str(rc).encode())
    retain("final_head", git("rev-parse", "HEAD"))
    retain("source_after", git("status", "--porcelain", "--untracked-files=no"))
    captured = report.capture(raw, events, Path("bazel-out"), WORK / "reports")
    retain("capture_exit", str(int(bool(captured["errors"]))).encode())
    (WORK / "context.json").write_text(json.dumps(context))
    (WORK / "owners").write_text("\n".join(TARGETS) + "\n")
    verdict = report.verdict(events, report.manifest_path(events), WORK / "context.json", WORK / "owners")
    RESULT["build"] = {k: verdict[k] for k in ("source", "status", "native_exit", "expected_labels",
                                               "errors", "missing", "failures", "execution_finished_at",
                                               "verdict_available_at")}
    save()
    assert verdict["status"] == "PASS" and set(verdict["expected_labels"]) == set(TARGETS)
    RESULT.update(status="PASS", phase="complete", verified_at=now())
    save()


if __name__ == "__main__":
    main()
