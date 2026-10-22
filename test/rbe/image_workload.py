"""Qualify the existing complete clang-tidy selection on both target CPUs."""
import json
import os
from pathlib import Path

import qualification_report as report


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    patterns = ["//test/...", "//tools/..."]
    options = ["--compilation_mode=opt", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc", "--keep_going",
               "--build_event_json_file_path_conversion=false",
               "--aspects=//tools/clang_tidy:clang_tidy.bzl%clang_tidy",
               "--output_groups=clang_tidy"]
    records = {"source": config["source"], "status": "INCOMPLETE", "architectures": [],
               "patterns": patterns, "scope": "Complete selection used by tools/lint.sh"}
    try:
        info = invoke("execution-root", ["info", "--lockfile_mode=error", "--noannounce_rc",
                      "execution_root"], "command", targets=[])
        if info["status"] != "PASS":
            raise RuntimeError("Cannot identify canonical execution root")
        root = Path((evidence / "invocation-execution-root/stdout.log").read_text().strip())
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("Invalid canonical execution root")
        guarded = invoke("execution-log-stream-guards",
                         ["run", "--lockfile_mode=error", "--noannounce_rc",
                          "@rules_python//python/bin:python", "--",
                          str(Path.cwd() / "test/rbe/image_execution_test.py")], "command", targets=[])
        if guarded["status"] != "PASS":
            raise RuntimeError("Execution-log streaming guards failed")
        for architecture in ("x86_64", "aarch64"):
            selected = [*options, "--config=" + architecture,
                        "--platforms=" + config["image_platforms"][architecture]]
            phase = "clang-tidy-full-" + architecture
            row = {"architecture": architecture, "status": "INCOMPLETE"}
            records["architectures"].append(row)
            try:
                analysis = invoke(phase + "-analysis", ["build", *selected, "--nobuild", *patterns],
                                  "analysis", targets=[])
                row["analysis_status"] = analysis["status"]
                if analysis["status"] != "PASS":
                    raise RuntimeError("Complete clang-tidy selection failed analysis")
                # Keep incomplete raw logs out of the compact evidence bundle.
                execution = private / (phase + "-execution.json")
                expected = evidence / ("invocation-" + phase + "-analysis") / "events.jsonl"
                built = invoke(phase, ["build", *selected, "--execution_log_json_file=" + str(execution),
                               "--noexecution_log_sort", *patterns], "build", (expected,), targets=[])
                row["build_status"] = built["status"]
                if execution.is_file():
                    record_execution(execution, architecture, "clang-tidy-full")
                if built["status"] != "PASS":
                    raise RuntimeError("Complete clang-tidy selection failed")
                actions = json.loads((evidence / (architecture + "-clang-tidy-full-image-execution.json")).read_text())
                matching = [action for action in actions["image_actions"] if action["mnemonic"] == "ClangTidy"]
                if not matching:
                    raise ValueError("Complete selection produced no clang-tidy actions")
                row["reports"] = []
                for action in matching:
                    if action["exitCode"] != 0 or action["status"] or not action["digest"]["hash"]:
                        raise ValueError("Clang-tidy action failed or omitted its digest")
                    match action["actualOutputs"]:
                        case [{"path": str(path), "digest": {"hash": str(digest), **details}}]:
                            expected_output = {"sha256": digest, "bytes": int(details.get("sizeBytes", 0))}
                        case _:
                            raise ValueError("Expected exactly one declared clang-tidy report")
                    relative = Path(path)
                    if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("bazel-out",):
                        raise ValueError("Unexpected configured clang-tidy report: " + path)
                    actual = report.file_record(root / relative)
                    if actual != expected_output:
                        raise ValueError("Clang-tidy report differs from native action identity: " + path)
                    row["reports"].append({"owner": action["targetLabel"], "action_digest": action["digest"],
                                           "cache_hit": action["cacheHit"], "runner": action["runner"],
                                           "path": path, **actual})
                row["action_count"] = len(matching)
                row["status"] = "PASS"
            except Exception as err:
                # Each architecture is independent; retain its failure and
                # collect the other result before failing the whole run.
                row["status"] = "FAIL"
                row["error"] = {"type": type(err).__name__, "message": str(err)}
        if any(row["status"] != "PASS" for row in records["architectures"]):
            raise RuntimeError("Complete clang-tidy qualification failed; see per-architecture results")
        records["status"] = "PASS"
    finally:
        report.write_json(evidence / "clang-tidy-full.json", records)
