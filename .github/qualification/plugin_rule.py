"""Link both plugin binaries on the exact combined amendment."""

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
FIXED = os.environ["FIXED"]
LIB = "@libpluginstack//:libpluginstack"
TARGETS = ["//runsc:runsc-plugin-stack", "//runsc/cmd/sentry:gvisor_sentry_plugin_stack"]
CONFIG = ["-c", "opt", "--config=plugin-tldk", "--lockfile_mode=off"]
BAZEL = ["docker", "exec", "plugin", str(WORK / "bazel")]
DEADLINE = time.monotonic() + 44 * 60
RESULT = {"status": "INCOMPLETE", "source": FIXED, "original_plugin_artifact": 13205762110,
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
    git("checkout", "--detach", FIXED)
    assert git("rev-parse", "HEAD").decode().strip() == FIXED
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
    RESULT.update(phase="links complete", links_verified_at=now())
    save()
    # The genrule invokes DPDK's own Makefiles; collect compile flags only
    # from actual Bazel C++ actions in this binary/sidecar closure.
    query = 'mnemonic("CppCompile|Genrule", deps(set(' + ' '.join(TARGETS) + ')))'
    with (WORK / "action-graph.json").open("wb") as out, (WORK / "action-graph.err").open("wb") as err:
        rc = subprocess.run(["timeout", "180", *BAZEL, "aquery", *CONFIG, "--output=jsonproto",
                             "--noinclude_artifacts", query], stdout=out, stderr=err).returncode
    RESULT["action_graph"] = {"exit": rc, "status": "INCOMPLETE"}
    save()
    assert rc == 0
    graph = json.loads((WORK / "action-graph.json").read_bytes())
    labels = {item["id"]: item["label"] for item in graph["targets"]}
    cxx = [action for action in graph["actions"] if action["mnemonic"] == "CppCompile"]
    dpdk, = [action for action in graph["actions"] if action["mnemonic"] == "Genrule"
             and labels[action["targetId"]].endswith("libpluginstack//:pluginstack_genrule")]
    dpdk_args = " ".join(dpdk["arguments"])
    assert "DPDK_MACHINE=ivb" in dpdk_args and "EXTRA_CFLAGS=" in dpdk_args and "-isystem" not in dpdk_args
    RESULT["action_graph"].update(status="PASS", dpdk=labels[dpdk["targetId"]],
                                  dpdk_make_receives_isystem=False, cpp_actions=len(cxx),
                                  cpp_owners=len({labels[a["targetId"]] for a in cxx}),
                                  cpp_with_system_include=sum(any(arg.startswith("-isystem") for arg in a["arguments"])
                                                              for a in cxx))
    RESULT.update(phase="complete", status="PASS", verified_at=now())
    save()


if __name__ == "__main__":
    main()
