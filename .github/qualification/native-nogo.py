"""Compare the Make arguments and run the two direct Bazel filter orders."""

import datetime
import json
import os
import shlex
import subprocess
import uuid
from pathlib import Path

import qualification_report as report

TMP = Path(os.environ["RUNNER_TEMP"])
PRODUCT = os.environ["PRODUCT"]
BASE = os.environ["BASE"]
OPTIONS = ["--test_tag_filters=nogo", "--build_tests_only"]
PATTERNS = ["//pkg/bits/...", "//pkg/tcpip/seqnum/..."]
NOGO = {"//pkg/bits:bits_nogo", "//pkg/bits:bits_test_nogo", "//pkg/tcpip/seqnum:seqnum_nogo"}
ORDINARY = {"//pkg/bits:bits_test"}
RESULT = {"source": PRODUCT, "parent": BASE, "harness": os.environ["GITHUB_SHA"], "verified": False}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save():
    data = json.dumps(RESULT, indent=2) + "\n"
    rc = (TMP / "nogo-resources.bazelrc").read_text()
    secrets = [line.split("x-buildbuddy-api-key=", 1)[1] for line in rc.splitlines() if "x-buildbuddy-api-key=" in line]
    assert secrets and all(key and key not in data for key in secrets)
    (TMP / "nogo-receipts/verdict.json").write_text(data)


def git(*args):
    return subprocess.check_output(["git", *args])


def make_arguments():
    recorder = TMP / "record-bazel"
    args = TMP / "make-arguments"
    recorder.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$RUNNER_TEMP/make-arguments"\n')
    recorder.chmod(0o700)
    original, candidate = [], []
    for source, command in [(BASE, original), (PRODUCT, candidate)]:
        git("checkout", "--quiet", "--detach", source)
        args.unlink(missing_ok=True)
        subprocess.run(["make", "-s", "nogo-tests", "DOCKER_BUILD=false", f"BAZEL={recorder}"], check=True)
        command.extend(args.read_text().splitlines())
    (selector,) = [line for line in Path(".bazelrc").read_text().splitlines() if line.startswith("test:nogo ")]
    assert shlex.split(selector)[1:] == OPTIONS
    at = candidate.index("--config=nogo")
    assert candidate[at + 1:] == ["//..."] and candidate[:at] + OPTIONS + candidate[at + 1:] == original
    assert all(candidate.count(flag) == 1 for flag in
               ("test", "--strip=never", "--remote_cache=grpcs://remote.buildbuddy.io", "--define=nogo_agent=quoted value"))
    RESULT["make"] = {"original": original, "proposed": candidate, "equivalent_expansion": True}


def run(name, filters, expected):
    directory = TMP / name
    directory.mkdir()
    events = directory / "raw.jsonl"
    args = ["test", "--nocache_test_results", "--flaky_test_attempts=1", "--runs_per_test=1",
            "--test_env=GO_TEST_WRAP_TESTV=1", "--nobuild_event_json_file_path_conversion",
            f"--build_event_json_file={events}", *filters, *PATTERNS]
    row = RESULT[name] = {"filters": filters, "patterns": PATTERNS, "expected": sorted(expected), "started_at": now()}
    (directory / "commit_file").write_bytes(git("cat-file", "commit", PRODUCT))
    (directory / "source_before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    (directory / "executed_arguments").write_text("\n".join(args) + "\n")
    owners = directory / "owners"
    owners.write_text("\n".join(sorted(expected)) + "\n")
    with (directory / "private-console").open("w") as out:
        code = subprocess.run(["timeout", "--kill-after=10s", "16m", "bazel",
                               f"--bazelrc={TMP / 'nogo-resources.bazelrc'}", *args],
                              stdout=out, stderr=subprocess.STDOUT).returncode
    row.update(native_exit=code, execution_finished_at=now())
    save()
    if events.exists():
        for line in events.open():
            event = json.loads(line)
            if "started" in event:
                row["invocation"] = str(uuid.UUID(event["started"]["uuid"]))
            if "optionsParsed" in event:
                row["build_tests_only_in_bep"] = "--build_tests_only" in event["optionsParsed"].get("cmdLine", [])
    if code:
        console = (directory / "private-console").read_text(errors="replace")
        row["diagnostic_classes"] = [word for word in (
            "Error downloading", "no such package", "GoStandardLibraryAnalysis", "GoCompilePkg", "NO_USABLE_STRATEGY_FOUND",
            "Could not find a matching execution platform", "No test targets were found", "Unrecognized option")
                                     if word in console]
    save()
    (directory / "native_exit").write_text(str(code))
    (directory / "source_after").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    (directory / "final_head").write_bytes(git("rev-parse", "HEAD"))
    clean = directory / "events.jsonl"
    captured = report.capture(events, clean, Path("bazel-out"), directory / "reports")
    (directory / "capture_exit").write_text("1" if captured["errors"] else "0")
    keys = ("executed_arguments", "commit_file", "source_before", "source_after", "native_exit", "capture_exit", "final_head")
    context = {key: key for key in keys}
    context.update(expected_source=PRODUCT, command_kind="test", execution_finished_at=row["execution_finished_at"])
    report.write_json(directory / "context.json", context)
    verdict = report.verdict(clean, report.manifest_path(clean), directory / "context.json", owners)
    row.update(shared_status=verdict["status"], counts=verdict["case_counts"], error_count=len(verdict["errors"]),
               missing_count=len(verdict["missing"]), verdict_at=verdict["verdict_available_at"], attempts=[])
    logs = {x["identity"]["label"]: x for x in captured["reports"] if x["name"] == "test.log"}
    xml = {x["identity"]["label"]: x for x in captured["reports"] if x["name"] == "test.xml"}
    for observed in verdict["executed"]:
        label = observed["owner"]["label"]
        assert label in expected
        log = logs[label]
        text = (directory / log["path"]).read_text()
        markers = ["amd64...PASS", "arm64...PASS"] if label in NOGO else ["--- PASS: TestTrailingZeros64"]
        execution = observed["execution_info"]
        row["attempts"].append({"label": label, "configuration": observed["owner"]["configuration"],
                                "run": observed["run"], "shard": observed["shard"], "attempt": observed["attempt"],
                                "status": observed["status"], "strategy": execution.get("strategy"),
                                "cached": bool(observed["cached_locally"] or execution.get("cachedRemotely")),
                                "markers": {m: m in text for m in markers}, "log_sha256": log["sha256"],
                                "xml_sha256": xml[label]["sha256"]})
    save()
    attempts = row["attempts"]
    assert code == 0 and verdict["status"] == "PASS" and not verdict["zero_case_owners"] and not verdict["skips"]
    assert len(attempts) == len(expected) and {a["label"] for a in attempts} == expected
    assert all(not a["cached"] and a["strategy"] in ("linux-sandbox", "processwrapper-sandbox", "sandboxed", "local")
               and a["status"] == "PASSED" and all(a["markers"].values()) for a in attempts)


def main():
    for name, fn in [("make_arguments", make_arguments),
                     ("nogo", lambda: run("nogo", ["--test_tag_filters=-nogo", "--config=nogo"], NOGO)),
                     ("ordinary", lambda: run("ordinary", ["--config=nogo", "--test_tag_filters=-nogo"], ORDINARY))]:
        try:
            fn()
        except Exception as error:
            RESULT.setdefault("failed_phases", {})[name] = type(error).__name__
        save()
    RESULT.update(verified=not RESULT.get("failed_phases"), finished_at=now())
    save()
    raise SystemExit(not RESULT["verified"])


if __name__ == "__main__":
    main()
