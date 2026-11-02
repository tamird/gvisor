"""Compare the public unit suite's selection and exercise its cgroup cache key."""
import datetime
import json
import os
from pathlib import Path
import re
import shlex
import subprocess

import qualification_report as report
import unit_matrix


TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "unit-results"
SOURCE = os.environ["PRODUCT"]
TARGETS = ["//pkg/buffer:buffer_test", "//runsc/cgroup:cgroup_test"]
TEST_TAG_FILTERS = ("-nogo", "-requires-kvm")


def git(*args):
    return subprocess.check_output(["git", *args])


def make_args(makefile):
    recorder = TMP / "record-bazel"
    recorder.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$RUNNER_TEMP/make-arguments\"\n")
    recorder.chmod(0o755)
    subprocess.run(["make", "-s", "-f", str(makefile), "unit-tests",
                    "DOCKER_BUILD=false", f"BAZEL={recorder}", f"REPO_DIR={Path.cwd()}"], check=True)
    return (TMP / "make-arguments").read_text().splitlines()


def run(name, args, *, analysis=False, mode):
    directory = OUT / name
    directory.mkdir()
    raw = TMP / (name + ".jsonl")
    position = args.index("--") if "--" in args else len(args)
    options = (["--nobuild"] if analysis else ["--test_env=GO_TEST_WRAP_TESTV=1", "--nocache_test_results",
               f"--target_pattern_file={TMP / 'focused-targets'}"])
    command = args[:position] + options + [f"--build_event_json_file={raw}"] + args[position:]
    (directory / "executed_arguments").write_text("\n".join(command) + "\n")
    (directory / "commit_file").write_bytes(git("cat-file", "commit", SOURCE))
    (directory / "source_before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    with (TMP / (name + ".console")).open("w") as console:
        completed = subprocess.run(["bazel", f"--bazelrc={TMP / 'unit-resources.bazelrc'}", *command],
                                   env=dict(os.environ, CGROUPV2=mode), stdout=console, stderr=subprocess.STDOUT)
    finished = datetime.datetime.now(datetime.timezone.utc).isoformat()
    (directory / "native_exit").write_text(str(completed.returncode))
    (directory / "source_after").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    (directory / "final_head").write_bytes(git("rev-parse", "HEAD"))
    if completed.returncode:
        print((TMP / (name + ".console")).read_text()[-8000:], flush=True)
    events = directory / "events.jsonl"
    captured = report.capture(raw, events, Path("bazel-out"), directory / "reports")
    if analysis:
        assert not captured["errors"], captured["errors"]
        selected = unit_matrix.configured_targets(str(raw), tests=True, allow_test_no_build=True,
                                                 expected_exit=completed.returncode)
        analyzed = selected
        selected = {label: target for label, target in analyzed.items()
                    if unit_matrix.test_tags_match(target.tags, TEST_TAG_FILTERS)}
        assert set(TARGETS) <= selected.keys()
        roots = []
        skipped = []
        with events.open() as source:
            for line in source:
                event = json.loads(line)
                if "configured" in event:
                    owner = event["id"]["targetConfigured"]
                    detail = event["configured"]
                    if not owner.get("aspect") and not detail.get("targetKind"):
                        raise ValueError(f"Missing kind for {owner}")
                    roots.append((owner["label"], owner.get("aspect", ""), detail.get("targetKind", ""), sorted(detail.get("tag", []))))
                if event.get("aborted", {}).get("reason") == "SKIPPED" and "targetCompleted" in event["id"]:
                    skipped.append(event["id"]["targetCompleted"]["label"])
        value = dict(analyzed_test_rules=sorted(analyzed), tests=sorted(selected),
                     test_tag_filters=TEST_TAG_FILTERS, filtered_tests=sorted(analyzed.keys() - selected.keys()),
                     configured=sorted(roots), skipped=sorted(skipped))
        assert not (directory / "source_before").read_bytes() and not (directory / "source_after").read_bytes()
        report.write_json(directory / "selected.json", value)
        return value
    (directory / "capture_exit").write_text("1" if captured["errors"] else "0")
    names = ("executed_arguments", "commit_file", "source_before", "source_after", "native_exit", "capture_exit", "final_head")
    context = {key: key for key in names}
    context.update(expected_source=SOURCE, command_kind="test", execution_finished_at=finished)
    report.write_json(directory / "context.json", context)
    v = report.verdict(events, report.manifest_path(events), directory / "context.json", TMP / "focused-targets", (),
                       test_tag_filters=TEST_TAG_FILTERS)
    report.write_json(directory / "verdict.json", v)
    assert v["status"] == "PASS", (v["errors"], v["missing"], v["failures"])
    assert not v["zero_case_owners"] and not v["all_skipped_owners"], (v["zero_case_owners"], v["all_skipped_owners"])
    cache = {}
    for line in events.read_text().splitlines():
        e = json.loads(line)
        if "testResult" in e:
            cache[e["id"]["testResult"]["label"]] = e["testResult"].get("cachedLocally", False)
    assert set(cache) == set(TARGETS)
    return cache


def main():
    assert git("rev-parse", "HEAD").decode().strip() == SOURCE
    unit, = [line for line in Path(".bazelrc").read_text().splitlines() if line.startswith("test:unit ")]
    assert [word for word in unit.split() if word.startswith("--test_tag_filters=")] == ["--test_tag_filters=" + ",".join(TEST_TAG_FILTERS)]
    actual = os.environ["CGROUPV2"]
    assert actual == ("true" if subprocess.check_output(["stat", "-f", "-c", "%T", "/sys/fs/cgroup"], text=True).strip() == "cgroup2fs" else "false")
    steps = []
    for file, expected in (("pipeline", 3), ("first-pass", 1)):
        text = Path(f".buildkite/{file}.yaml").read_text()
        matches = re.findall(r'(?m)^    label: "[^"\n]*Unit tests [^"\n]*"\n    command: (.+)\n    env:\n      DOCKER_BUILD: "false"$', text)
        assert len(matches) == expected
        steps.extend(matches)
    assert len(set(steps)) == 1
    executable, *native = shlex.split(steps[0])
    assert executable == "bazel" and native[0] == "test"
    (TMP / "old.Makefile").write_bytes(git("show", "HEAD^:Makefile"))
    (TMP / "focused-targets").write_text("\n".join(TARGETS) + "\n")
    selected = {}
    receipt = {"source": SOURCE, "actual_cgroup_v2": actual, "expected_targets": TARGETS,
               "scope": "full target analysis and focused execution", "errors": []}
    for name, args in (("legacy", make_args(TMP / "old.Makefile")), ("make", make_args("Makefile")), ("native", native)):
        try:
            selected[name] = run(name, args, analysis=True, mode=actual)
        except Exception as error:
            receipt["errors"].append(f"{name} selection: {error!r}")
    if len(selected) == 3:
        baseline = selected["legacy"]
        receipt["configured_unit_tests"] = len(baseline["tests"])
        receipt["configured_records"] = len(baseline["configured"])
        for name, result in selected.items():
            if result != baseline:
                receipt["errors"].append(f"{name} differs from the Make selection")
    receipt["prior_cache_evidence"] = "https://github.com/tamird/gvisor/actions/runs/41493261450/artifacts/13166725105"
    try:
        seen = run("named-cases", native, mode=actual)
        if any(seen.values()):
            raise ValueError("Expected fresh test results from both packages")
    except Exception as error:
        receipt["errors"].append(f"named cases: {error!r}")
    receipt["verified"] = not receipt["errors"]
    report.write_json(OUT / "verdict.json", receipt)
    if receipt["errors"]:
        raise RuntimeError(receipt["errors"])


if __name__ == "__main__":
    main()
