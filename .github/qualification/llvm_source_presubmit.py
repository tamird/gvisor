"""Qualify the unmodified compiler integration with the current Make unit-test selection."""
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SOURCE = "48f457b4e040312422b8e28b97081998d1d81297"
ROOT = Path(os.environ["RUNNER_TEMP"]) / "llvm-source-presubmit"
CONTROL = "//test/syscalls:uname_test_native"
PUBLIC = ROOT / "results"


def make_targets() -> list[str]:
    # Read the arguments from the recipe at this exact commit. test/runner/... is
    # present here and was absent in the earlier presubmit.
    lines = Path("Makefile").read_text().splitlines()
    recipe = lines[1 + next(i for i, s in enumerate(lines) if s.startswith("unit-tests:"))]
    assert "--test_tag_filters=-nogo$(COMMA)-requires-kvm" in recipe, recipe
    assert "--build_tag_filters=-network_plugins" in recipe, recipe
    assert "--test_env=CGROUPV2=$(CGROUPV2)" in recipe, recipe
    _, targets = recipe.removesuffix(")").split(" -- ")
    return [s if s.startswith("//") else "//" + s for s in targets.split()]


def write_result(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n")


def read_bep(path: Path) -> dict:
    digest = hashlib.sha256()
    configs, ends, tests, phases, failures, shards = {}, {}, {}, {}, [], []
    configured = set()
    if path.exists():
        with path.open("rb") as stream:
            for line in stream:
                digest.update(line)
                event = json.loads(line)
                ident = event["id"]
                if "started" in event:
                    phases["invocation"] = event["started"].get("uuid")
                if "configuration" in event:
                    config = event["configuration"]
                    configs[ident["configuration"]["id"]] = {
                        k: config[k] for k in ("cpu", "mnemonic", "platformName", "isTool") if k in config
                    }
                if "configured" in event:
                    configured.add(ident["targetConfigured"]["label"])
                if "finished" in event:
                    phases["bazel_exit"] = event["finished"].get("exitCode")
                if "targetCompleted" in ident and not (end := ident["targetCompleted"]).get("aspect"):
                    if "completed" in event:
                        state = "built" if event["completed"].get("success", False) else "failed"
                    elif "aborted" in event:
                        state = event["aborted"].get("reason", "unspecified")
                    else:
                        continue
                    ends.setdefault(end["label"], []).append([end["configuration"]["id"], state])
                if "testSummary" in event:
                    record = event["testSummary"]
                    tests[ident["testSummary"]["label"]] = [record.get("overallStatus"), record.get("totalRunCount")]
                if "testResult" in event:
                    ref, result = ident["testResult"], event["testResult"]
                    info = result.get("executionInfo", {})
                    shards.append([ref["label"], ref.get("shard"), result.get("status"), info.get("strategy"),
                                   info.get("hostname"), result.get("cachedLocally", False), info.get("cachedRemotely", False)])
                if "action" in event and not event["action"].get("success", False) and len(failures) < 20:
                    action = event["action"]
                    failures.append([ident.get("actionCompleted", {}).get("label"),action.get("type"),
                                     action.get("exitCode"), action.get("failureDetail",{}).get("spawn",{}).get("code")])
    return dict(**phases, digest=digest.hexdigest(), configurations=configs, targets=ends,
                configured_labels=sorted(configured), tests=tests, shards=shards, failed_actions=failures)


def submit(arch: str) -> bool:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    patterns = make_targets() + [CONTROL]
    cpu = {"amd64": "x86_64", "arm64": "aarch64"}[arch]
    platform = "//tools/bazeldefs:linux_" + arch
    java = ["--host_jvm_args=-Xmx10g", "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(ROOT / "remote.rc")]
    common = ["--config=" + cpu, "--host_platform=" + platform, "--extra_execution_platforms=" + platform,
              "--remote_default_exec_properties=Arch=" + arch, "--distdir=" + str(ROOT / "dist"),
              "--keep_going", "--strip=never", "--test_env=CGROUPV2=true", "--test_env=GO_TEST_WRAP_TESTV=1",
              "--test_tag_filters=-nogo,-requires-kvm", "--build_tag_filters=-network_plugins",
              "--nocache_test_results", "--flaky_test_attempts=1", "--jobs=64"]
    records = {}
    for name, flags in [("all", ["--nobuild"]), ("tests", ["--nobuild", "--build_tests_only"]), ("execution", [])]:
        bep = ROOT / f"{arch}-{name}.bep"
        log = ROOT / f"{arch}-{name}.log"
        with log.open("w") as out:
            rc = subprocess.run([str(ROOT / "bazel"), *java, "test", *common, *flags,
                                 "--build_event_json_file=" + str(bep), "--", *patterns],
                                stdout=out, stderr=subprocess.STDOUT).returncode
        record = read_bep(bep)
        record["exit"] = rc
        # Limit diagnostics to Bazel errors and redact common authentication
        # parameters. All raw execution logs and the full BEP stay on the runner.
        errors = []
        for line in log.open(errors="replace"):
            if len(errors) < 20 and (line.startswith("ERROR:") or "fatal error:" in line or "error:" in line):
                if not re.search(r"credential|token|header|authorization|secret|api.key", line, re.I):
                    errors.append(line.strip()[:550])
        record["diagnostics"] = errors
        write_result(PUBLIC / f"{arch}-{name}.json", record)
        records[name] = record
        print(arch, name, rc, record.get("invocation"), len(record["targets"]), len(record["shards"]), flush=True)
        bep.unlink(missing_ok=True)
    roots = {k for k,v in records["all"]["targets"].items() if any(state == "NO_BUILD" for _,state in v)}
    runnable = {k for k,v in records["tests"]["targets"].items() if any(state == "NO_BUILD" for _,state in v)}
    execution = records["execution"]
    original = Path(".github/qualification/original-unbuilt-tests.txt").read_text().splitlines()
    drift = {k: [execution["targets"].get(k,[]), execution["tests"].get(k)] for k in original}
    missing = sorted(roots - execution["targets"].keys())
    unreported = sorted(runnable - execution["tests"].keys())
    roles = {k: "test" if k in runnable else "build" for k in sorted(roots)}
    report = dict(source=SOURCE, arch=arch, patterns=patterns, roles=roles, baseline=drift,
                  missing=missing, missing_test_summaries=unreported, expected_control=CONTROL,
                  target_status_counts=dict(collections.Counter(v for k in roots for _,v in execution["targets"].get(k,[]))),
                  shard_status_counts=dict(collections.Counter(s[2] for s in execution["shards"])))
    write_result(PUBLIC / f"{arch}-accounting.json", report)
    return (bool(roots) and runnable <= roots and CONTROL in runnable and not missing and not unreported
            and all(row and all(state == "built" for _,state in row) for row in (execution["targets"].get(k) for k in roots))
            and bool(execution["shards"]) and execution["exit"] == 0)


if __name__ == "__main__":
    if subprocess.check_output(["git","rev-parse","HEAD^"], text=True).strip() != SOURCE:
        raise SystemExit("unexpected source parent")
    raise SystemExit(0 if submit(sys.argv[1]) else 1)
