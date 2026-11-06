"""Compare Make and RBE target selections without running the unit suite."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess

import unit_matrix

PUBLIC = "f51a205a8f4afbc5ab41aded070df74951b081ea"
FORK = "9fd10bc0c7a6d2ccc8641491b3539f3cc867943e"
TEMP = Path(os.environ["RUNNER_TEMP"])
OUT = TEMP / "source-unit-selection"
RC = TEMP / "source-unit-analysis.bazelrc"


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def checkout(source):
    subprocess.run(["git", "checkout", "--quiet", "--detach", source], check=True)
    assert output("git", "rev-parse", "HEAD") == source
    assert not output("git", "status", "--porcelain", "--untracked-files=no")
    version = output("bazel", "--version")
    assert version == "bazel " + Path(".bazelversion").read_text().strip(), version
    return version


def make_arguments():
    # Use the public recipe's real arguments, including its cgroup detection.
    recorder = TEMP / "record-unit-arguments"
    recorder.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$RUNNER_TEMP/make-arguments\"\n")
    recorder.chmod(0o755)
    subprocess.run(["make", "-s", "unit-tests", "DOCKER_BUILD=false", f"BAZEL={recorder}"], check=True)
    args = (TEMP / "make-arguments").read_text().splitlines()
    assert args[0] == "test" and "--test_env=CGROUPV2=true" in args and "--" in args, args
    return args


def selection(name, source, args, *, tests_only):
    bep = TEMP / (name + ".bep")
    log = TEMP / (name + ".log")
    args = [a for a in args if not a.startswith("--build_event_json_file=")]
    position = args.index("--") if "--" in args else 1
    options = ["--nobuild", f"--build_event_json_file={bep}"]
    if tests_only:
        options.append("--build_tests_only")
    cmd = ["bazel", f"--bazelrc={RC}", *args[:position], *options, *args[position:]]
    with log.open("w") as stream:
        code = subprocess.run(cmd, stdout=stream, stderr=subprocess.STDOUT).returncode
    details = {"source": source, "arguments": args, "tests_only": tests_only, "bazel_exit": code}
    if bep.is_file():
        details["bep_sha256"] = hashlib.sha256(bep.read_bytes()).hexdigest()
        with bep.open() as events, gzip.open(OUT / (name + "-selection.jsonl.gz"), "wt") as capture:
            configurations = {}
            incomplete = []
            for line in events:
                event = json.loads(line)
                if any(key in event for key in ("configured", "configuration", "aborted", "finished", "testResult", "testSummary")):
                    capture.write(line)
                if "configuration" in event:
                    configurations[event["id"]["configuration"]["id"]] = event["configuration"]
                if "aborted" in event and event["aborted"].get("reason") not in ("NO_BUILD", "SKIPPED") and len(incomplete) < 5:
                    incomplete.append([event["id"], event["aborted"]])
                if "finished" in event:
                    details["finished"] = event["finished"].get("exitCode")
        details.update(configurations=configurations, incomplete=incomplete)
    write(OUT / (name + "-status.json"), details)
    try:
        all_roots = unit_matrix.configured_targets(str(bep), tests=None, allow_test_no_build=True, expected_exit=code)
        tests = unit_matrix.configured_targets(str(bep), tests=True, allow_test_no_build=True, expected_exit=code)
    except (ValueError, OSError) as exc:
        errors = [line[:1400] for line in log.read_text(errors="replace").splitlines()
                  if "error" in line.lower() or "failed" in line.lower()]
        details.update(error=str(exc)[:3500], console_errors=errors[:9])
        write(OUT / (name + "-status.json"), details)
        print(name, details["error"], errors[:4], flush=True)
        return False
    result = {label: {"configuration": target.configuration, "type": "test rule" if label in tests else "other rule",
                      "tags": target.tags, "aspects": target.aspects}
              for label, target in all_roots.items()}
    if tests_only:
        assert all_roots.keys() == tests.keys(), "Bazel included a non-test in its test-only selection"
        if name != "mixed-tests":
            for label, target in tests.items():
                assert not {"nogo", "requires-kvm"}.intersection(target.tags), label
    write(OUT / (name + "-roots.json"), result)
    print(name, "configured", len(tests), "test and", len(all_roots) - len(tests), "build-only owners", flush=True)
    return True


def profile(name, source, args):
    # NO_BUILD alone lists build-only Nogo targets as test rules. Ask Bazel for
    # its tests-to-run separately instead of classifying from testSize or tags.
    complete = selection(name, source, args, tests_only=False)
    runnable = selection(name + "-tests", source, args, tests_only=True)
    return complete and runnable


def main():
    OUT.mkdir(exist_ok=True)
    status = {"host_cpu": output("uname", "-m"), "host_cgroup": output("stat", "-fc", "%T", "/sys/fs/cgroup")}
    write(OUT / "comparison.json", status)
    assert status == {"host_cpu": "x86_64", "host_cgroup": "cgroup2fs"}
    # Keep the RBE target and execution platforms. No worker or remote service
    # is needed to register those platforms and analyze the target graph.
    RC.write_text("build --jobs=3\nbuild:buildbuddy_remote_executor --remote_cache=\n")
    status["public_bazel"] = checkout(PUBLIC)
    public_args = make_arguments()
    success = profile("upstream", PUBLIC, public_args)
    subprocess.run(["bazel", "shutdown"], check=True)
    status["fork_bazel"] = checkout(FORK)
    steps = Path(".buildkite/rbe-source.yaml").read_text().splitlines()
    mixed = shlex.split(next(line.split("command:", 1)[1] for line in steps if "command:" in line))
    assert mixed == ["bazel", "test", "--config=rbe-matrix", "//test/rbe:mixed_execution_smoke"], mixed
    # The RBE profile normally requires remote strategies. Nothing executes
    # with --nobuild, so permit analysis on the ordinary GitHub Linux runner.
    strategy = ["--spawn_strategy=local", "--strategy=TestRunner=local"]
    common = ["test", "--config=rbe-matrix", "--config=x86_64", "--strip=never", *strategy]
    plans = {"mixed": [*mixed[1:-1], *strategy, mixed[-1]], "fork-unit": [*common, "--config=unit"],
             "fork-public-roots": ["test", "--config=rbe-matrix", "--config=x86_64", *strategy, *public_args[1:]]}
    for name, args in plans.items():
        passed = profile(name, FORK, args)
        success = success and passed
    status["successful"] = success
    write(OUT / "comparison.json", status)
    if not success:
        raise RuntimeError("Inspect phase status and the preserved available owner lists")


if __name__ == "__main__":
    main()
