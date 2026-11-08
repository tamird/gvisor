"""Compare the uploaded source selectors and run the remaining license job."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

import qualify_licenses as licenses

WORK = Path(__file__).resolve().parent
OUTPUT = WORK / "result" / "selection.json"
SOURCE = "bb16ce17359ed91e9306138d3ac75fbaff367958"
PREVIOUS = "c68b01cd010e8cf58be88f51012ac2ee5351a46b"
PREDICATE = 'build.env("STAGED_BINARIES") == null'
EXPECTED = {"//:governance-regen_0_test", "//:governance-regen_1_test"}
VERIFY = "make license-check"
UNIT = "make unit-tests"
GOVERNANCE = "make governance-check"
result = {"status": "INCOMPLETE", "source": SOURCE, "previous": PREVIOUS, "pipelines": {}}


def save():
    OUTPUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


def run(args, **kw):
    return subprocess.check_output(args, text=True, **kw)


def render(path, env):
    return json.loads(run(["agent", "pipeline", "upload", "--dry-run", "--format", "json",
                           "--agent-access-token", "offline", str(path)], env=env))


def command(step):
    return step.get("command") if isinstance(step, dict) else None


def compare(filename, units):
    rows = result["pipelines"][filename] = {}
    lic_env = None
    for mode in ("source", "staged"):
        env = {**os.environ, "BUILDKITE_BRANCH": "master"}
        env.pop("STAGED_BINARIES", None)
        if mode == "staged":
            env["STAGED_BINARIES"] = "fixture"
        previous = render(WORK / filename, env)
        current = render(Path(".buildkite") / filename, env)
        removed, = (s for s in previous["steps"] if command(s) == GOVERNANCE)
        assert removed["if"] == PREDICATE
        previous["steps"].remove(removed)
        assert current == previous and all(command(s) != GOVERNANCE for s in current["steps"])
        keep = [s for s in current["steps"] if command(s) in (UNIT, VERIFY)]
        assert all(s["if"] == removed["if"] for s in keep)
        license_step, = (s for s in keep if command(s) == VERIFY)
        selected_units = [s for s in keep if command(s) == UNIT]
        assert len(selected_units) == units
        assert license_step["env"]["DOCKER_BUILD"] == "false"
        lic_env = license_step["env"]["DOCKER_BUILD"]
        rows[mode] = {
            "original_governance_jobs": 1, "remaining_governance_jobs": 0,
            "retained_license": [command(license_step), lic_env],
            "unit_jobs": [{"label": s["label"], "agents": s.get("agents", {})} for s in selected_units],
            "if": removed["if"], "selected_by_if": "STAGED_BINARIES" not in env,
            "all_other_rendered_steps_equal": True,
        }
        save()
    return lic_env


def check_owners(rc):
    # Read the recipe that each retained unit job invokes, without building it.
    preview = run(["make", "--no-print-directory", "-n", "unit-tests"], env={**os.environ, "DOCKER_BUILD": "false"})
    line, = (line for line in preview.splitlines() if "--test_tag_filters=-nogo,-requires-kvm" in line)
    argv = shlex.split(line)
    assert "//:all" in argv and "--build_tests_only" not in argv
    assert "--build_tag_filters=-network_plugins" in argv
    query = ["bazel", "--bazelrc=" + str(rc), "query", "--output=label"]
    suite = set(run([*query, "tests(//:governance-check)"]).splitlines())
    # Check both the wildcard and the exclusions from the actual unit-test command.
    owners = "tests(//:all) intersect tests(//:governance-check)"
    filtered = f'({owners}) except attr(tags, "manual|nogo|requires-kvm|network_plugins", ({owners}))'
    wildcard = set(run([*query, filtered]).splitlines())
    assert suite == wildcard == EXPECTED, (suite, wildcard)
    result["owners"] = {"dedicated_suite": sorted(suite), "unit_wildcard": sorted(wildcard),
                        "prior_executed_shards_artifact": "13572296525"}
    save()


def verify_licenses(rc):
    catalog = Path("tools/licensecheck/dependencies.yaml")
    args = [*shlex.split(VERIFY), "STARTUP_OPTIONS=--bazelrc=" + str(rc)]
    clean = licenses.phase("make", args, WORK, SOURCE, None)
    result["clean"] = clean
    save()
    assert clean["status"] == "PASS" and clean["counts"] == [220], clean
    events = [json.loads(line) for line in (WORK / "build.events").open()]
    commands = [e["started"]["command"] for e in events if "started" in e]
    complete = [e for e in events if e["id"].get("targetCompleted", {}).get("label") == licenses.MAIN]
    assert commands == ["build"] and len(complete) == 1 and complete[0]["completed"]["success"]
    result["license_build"] = {"commands": commands, "owner": licenses.MAIN}
    save()
    content = catalog.read_bytes()
    changed, count = re.subn(rb"(?ms)^- dependency: abseil-cpp\n.*?(?=^- dependency: )", b"", content)
    assert count == 1
    try:
        catalog.write_bytes(changed)
        control = licenses.phase("negative", args, WORK, SOURCE, None)
        diagnostics = (WORK / "negative" / "stderr.log").read_bytes().splitlines()
        missing = diagnostics.count(b"missing entry for abseil-cpp")
        result["omitted_entry"] = {"exit": control["native_exit"], "missing_abseil_cpp": missing,
                                   "original_catalog_sha256": hashlib.sha256(content).hexdigest()}
        save()
        assert control["native_exit"] == 2 and missing == 1
    finally:
        catalog.write_bytes(content)


def main():
    assert run(["git", "rev-parse", "HEAD"]).strip() == SOURCE
    for name, count in (("first-pass.yaml", 1), ("pipeline.yaml", 3)):
        os.environ["DOCKER_BUILD"] = compare(name, count)
    rc = WORK / "local.bazelrc"
    rc.write_text(
        "common --remote_cache= --remote_executor= --bes_backend=\n"
        "build --jobs=4 --spawn_strategy=processwrapper-sandbox,local\n"
        "build --strategy=GoStdlib=processwrapper-sandbox --strategy=GoStdlibList=processwrapper-sandbox\n"
        f"build --build_event_json_file={WORK}/build.events\n"
        f"cquery --build_event_json_file={WORK}/cquery.events\n"
    )
    check_owners(rc)
    verify_licenses(rc)
    result["status"] = "PASS"


try:
    main()
except Exception as error:
    result["status"] = "FAIL"
    result["failure"] = {"type": type(error).__name__, "line": error.__traceback__.tb_lineno}
    raise
finally:
    save()
