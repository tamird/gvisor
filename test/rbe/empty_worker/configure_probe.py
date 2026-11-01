"""Qualify direct compiler probes against the same-version scratch control."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


root = Path(os.environ["RUNNER_TEMP"])
workspace = Path(os.environ["GITHUB_WORKSPACE"])
source = "7dc8df3c4bb7da1bc04037fe9a2d641cb36cc5a4"
os.environ["PRODUCT"] = source
import wildcard
supplier = workspace / "supplier"
control = "f0a9efec3c81f0b64489dd811567b3ee5b6fb25a"
candidate = "b0b4b6c173c4312c14f3c5a427e9e3cb7271143c"
registry = "f8f5f201805db9b0c27571f7630cb8a37f3a78d6"
target = "@@rules_cc_autoconf+//gnulib/m4/stdbool:stdbool"
row = {"status": "INCOMPLETE", "source": source, "registry": registry,
       "target": target, "executed_tests": 0, "arms": {},
       "supplier_control": control, "supplier_candidate": candidate, "supplier_version": "0.35.0"}
out = root / "package-evidence"
base = ["bazel", "--bazelrc=" + str(root / "sidecar.bazelrc")]


def save():
    (out / "configure-probe.json").write_text(json.dumps(row, indent=2) + "\n")


def invoke(item, phase, command, output=None):
    log = output or root / (item["name"] + "-" + phase + ".private")
    result = {"started": wildcard.now()}
    item[phase] = result
    save()
    with log.open("w") as stream, (root / (item["name"] + "-" + phase + "-stderr.private")).open("w") as err:
        result["exit"] = subprocess.run(["timeout", "--signal=INT", "--kill-after=25s", "12m", *command], stdout=stream, stderr=err).returncode
    result["finished"] = wildcard.now()
    save()
    return result["exit"]


def collect(name, revision, image, isolation, platform):
    item = {"name": name, "status": "INCOMPLETE", "image": image, "isolation": isolation, "supplier": revision}
    row["arms"][name] = item
    folder = out / name
    folder.mkdir(exist_ok=True)
    flags = ["--config=x86_64", "--config=plugin-tldk", "--compilation_mode=opt",
             "--host_platform=" + platform, "--extra_execution_platforms=" + platform,
             "--registry=" + (workspace / "registry").as_uri(), "--registry=https://bcr.bazel.build",
             "--action_env=RULES_CC_AUTOCONF_DEBUG=debug",
             "--jobs=400", "--remote_download_outputs=all", "--color=no", "--curses=no",
             "--output_groups=autoconf_checks,autoconf_results"]
    bep = root / (name + ".bep")
    compact = root / (name + ".compact")
    try:
        subprocess.run(["git", "checkout", "--quiet", "--detach", revision], cwd=supplier, check=True)
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=supplier, text=True).strip() == revision
        rc = invoke(item, "build", [*base, "build", *flags, "--build_event_json_file=" + str(bep), "--execution_log_compact_file=" + str(compact), target])
        parsed = wildcard.events(bep)
        item["build"].update(invocation=parsed["invocation"], finish=parsed["finish"], completed=[{"label": label, "configuration": config, "status": status} for (label, config), status in parsed["completed"].items()])
        save()
        item["actions"] = wildcard.stats(compact, "amd64", folder, image, isolation, target)
        assert rc == 0 and len(parsed["completed"]) == 1 and all(label == target and status == "built" for (label, _), status in parsed["completed"].items())
        listing = root / (name + "-outputs.private")
        assert invoke(item, "outputs", [*base, "cquery", *flags, target, "--output=files"], listing) == 0
        paths = [Path(value) for value in listing.read_text().splitlines()]
        assert paths and all(str(path).startswith("bazel-out/") and "/gnulib/m4/stdbool/" in str(path) for path in paths)
        specs = {path: json.loads(path.read_text()) for path in paths if path.name.endswith(".check.json")}
        selected = {spec.get("define") or spec.get("name"): (path, spec) for path, spec in specs.items() if spec.get("define") == "HAVE_STDBOOL_H" or spec.get("name") == "ac_cv_type__Bool"}
        assert set(selected) == {"HAVE_STDBOOL_H", "ac_cv_type__Bool"}
        observations = {}
        for key, (path, spec) in selected.items():
            result = path.with_name(path.name.replace(".check.json", ".result.cache.json"))
            assert result in paths
            data = result.read_bytes()
            observations[key] = {"result": json.loads(data), "sha256": hashlib.sha256(data).hexdigest(), "path": str(result), "check_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            (folder / result.name).write_bytes(data)
        item["checks"] = observations
        config_path = next(iter(selected.values()))[0].parent.parent / "stdbool.ac.json"
        config = json.loads(config_path.read_text())
        item["compiler"] = {key: config[key] for key in ("c_compiler", "c_flags", "compiler_type")}
        item["probe_actions"] = [action for action in json.loads((folder / "actions.json").read_text())["owner_actions"] if action["m"] == "CcAutoconfCheck"]
        assert item["actions"]["decoder_exits"] == [0, 0] and not item["actions"]["off_platform"] and not item["actions"]["local_runs"]
        assert len(item["probe_actions"]) >= 2 and all(action["r"] == "remote" and not action["c"] and action["s"] in (None, "") and action["e"] in (None, 0) for action in item["probe_actions"])
        assert all(value["result"]["success"] is (name == "fixed") for value in observations.values())
        item["status"] = "OBSERVED"
    except Exception as err:
        item["collection_error_type"] = type(err).__name__
        if any(phase.get("exit", 0) != 0 for phase in (item.get("build", {}), item.get("outputs", {}))):
            item["status"] = "FAIL"
    item["verified_at"] = wildcard.now()
    save()



def checked_cases(xml):
    doc = ET.fromstring(xml)
    cases = list(doc.iter("testcase"))
    assert cases and all(not any(case.find(tag) is not None for tag in ("skipped", "failure", "error")) and case.get("status") != "notrun" and case.get("result") not in ("suppressed", "skipped") for case in cases)
    return [{"class": case.get("classname"), "name": case.get("name")} for case in cases]


def unit_tests():
    labels = {"@@rules_cc_autoconf+//autoconf/private/checker:" + name for name in ("process_test", "system_header_test", "library_check_test")}
    image = "docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a"
    item = {"name": "unit", "status": "INCOMPLETE", "expected_targets": sorted(labels), "tests": {}}
    row["unit"] = item
    folder = out / "unit"
    folder.mkdir(exist_ok=True)
    bep, compact = root / "unit.bep", root / "unit.compact"
    try:
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=supplier, text=True).strip() == candidate
        flags = ["--config=x86_64", "--config=plugin-tldk", "--compilation_mode=opt", "--host_platform=//test/rbe/make_control:populated", "--extra_execution_platforms=//test/rbe/make_control:populated", "--registry=" + (workspace / "registry").as_uri(), "--registry=https://bcr.bazel.build", "--jobs=400", "--remote_download_outputs=all", "--test_strategy=remote", "--nocache_test_results", "--keep_going", "--color=no", "--curses=no", "--build_event_json_file=" + str(bep), "--execution_log_compact_file=" + str(compact)]
        rc = invoke(item, "test", [*base, "test", *flags, *sorted(labels)])
        parsed = wildcard.events(bep)
        item["test"].update(invocation=parsed["invocation"], finish=parsed["finish"], completed=[{"label": label, "configuration": config, "status": status} for (label, config), status in parsed["completed"].items()])
        summaries = {}
        results = {}
        for line in bep.read_text().splitlines():
            event = json.loads(line)
            if "testSummary" in event["id"]:
                label = event["id"]["testSummary"]["label"]
                summaries[label] = event["testSummary"].get("overallStatus")
            if "testResult" in event["id"]:
                label = event["id"]["testResult"]["label"]
                results.setdefault(label, []).append(event["testResult"].get("status"))
        item["summaries"], item["attempts"] = summaries, results
        save()
        item["actions"] = wildcard.stats(compact, "amd64", folder, image, "firecracker", labels)
        actions = json.loads((folder / "actions.json").read_text())["owner_actions"]
        item["test_actions"] = [action for action in actions if action["m"] == "TestRunner"]
        for label in sorted(labels):
            name = label.split(":")[-1]
            paths = Path("bazel-testlogs/external/rules_cc_autoconf+/autoconf/private/checker") / name
            xml, log = (paths / "test.xml").read_bytes(), (paths / "test.log").read_bytes()
            (folder / (name + ".xml")).write_bytes(xml)
            (folder / (name + ".log")).write_bytes(log)
            item["tests"][label] = {"xml_sha256": hashlib.sha256(xml).hexdigest(), "log_sha256": hashlib.sha256(log).hexdigest(), "cases": checked_cases(xml)}
            if name == "process_test":
                assert b"process_test: argv, streams, exit, signal and launch errors PASSED" in log
        save()
        assert rc == 0 and set(parsed["configured"]) == labels and len(parsed["completed"]) == len(labels) and all(label in labels and status == "built" for (label, _), status in parsed["completed"].items())
        assert summaries == dict.fromkeys(labels, "PASSED") and results == {label: ["PASSED"] for label in labels}
        assert item["actions"]["decoder_exits"] == [0, 0] and not item["actions"]["off_platform"] and not item["actions"]["local_runs"]
        assert len(item["test_actions"]) == len(labels) and {action["l"] for action in item["test_actions"]} == labels
        assert all(action["r"] == "remote" and not action["c"] and action["s"] in (None, "") and action["e"] in (None, 0) for action in item["test_actions"])
        row["executed_tests"] = len(labels)
        item["status"] = "PASS"
    except Exception as err:
        item["collection_error_type"] = type(err).__name__
        if item.get("test", {}).get("exit", 0):
            item["status"] = "FAIL"
    item["verified_at"] = wildcard.now()
    save()


save()
try:
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace / "registry", text=True).strip() == registry
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=supplier, text=True).strip() == candidate
    assert checked_cases('<testsuite><testcase name="positive"/></testsuite>') == [{"class": None, "name": "positive"}]
    for xml in ('<testsuite/>', '<testsuite><testcase><failure/></testcase></testsuite>', '<testsuite><testcase status="notrun"/></testsuite>', '<testsuite><testcase><skipped/></testcase></testsuite>'):
        try:
            checked_cases(xml)
        except AssertionError:
            continue
        raise AssertionError("case verifier accepted missing or failed execution")
    row["reporter_controls"] = 5
    row["format"] = {"name": "format"}
    invoke(row["format"], "check", [*base, "run", "@buildifier_prebuilt//:buildifier", "--", "-mode=check", str(supplier / "autoconf/private/checker/BUILD.bazel")])
    # This is a qualification-only dependency override, identical in both arms.
    with Path("MODULE.bazel").open("a") as stream:
        stream.write('\nlocal_path_override(module_name = "rules_cc_autoconf", path = ' + json.dumps(str(supplier)) + ')\n')
    collect("control", control, wildcard.image, "oci", "//test/rbe/empty_worker:amd64_musl")
    collect("fixed", candidate, wildcard.image, "oci", "//test/rbe/empty_worker:amd64_musl")
    unit_tests()
    if all(value["status"] == "OBSERVED" for value in row["arms"].values()) and row["unit"]["status"] == "PASS" and row["format"]["check"]["exit"] == 0:
        assert row["arms"]["control"]["compiler"] == row["arms"]["fixed"]["compiler"]
        assert {key: value["check_sha256"] for key, value in row["arms"]["control"]["checks"].items()} == {key: value["check_sha256"] for key, value in row["arms"]["fixed"]["checks"].items()}
        row["status"] = "PASS"
except Exception as err:
    row["error_type"] = type(err).__name__
finally:
    row["verified_at"] = wildcard.now()
    save()
print(row["status"], flush=True)
sys.exit(row["status"] != "PASS")
