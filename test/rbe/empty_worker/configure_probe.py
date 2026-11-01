"""Compare one unchanged supplier configure target on two execution roots."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


root = Path(os.environ["RUNNER_TEMP"])
workspace = Path(os.environ["GITHUB_WORKSPACE"])
source = "7dc8df3c4bb7da1bc04037fe9a2d641cb36cc5a4"
os.environ["PRODUCT"] = source
import wildcard
registry = "f8f5f201805db9b0c27571f7630cb8a37f3a78d6"
target = "@@rules_cc_autoconf+//gnulib/m4/stdbool:stdbool"
row = {"status": "INCOMPLETE", "source": source, "registry": registry,
       "target": target, "executed_tests": 0, "arms": {}}
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


def collect(name, image, isolation, platform):
    item = {"name": name, "status": "INCOMPLETE", "image": image, "isolation": isolation}
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
        assert all(value["result"]["success"] is (name == "populated") for value in observations.values())
        item["status"] = "OBSERVED"
    except Exception as err:
        item["collection_error_type"] = type(err).__name__
        if any(phase.get("exit", 0) != 0 for phase in (item.get("build", {}), item.get("outputs", {}))):
            item["status"] = "FAIL"
    item["verified_at"] = wildcard.now()
    save()


save()
try:
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace / "registry", text=True).strip() == registry
    collect("scratch", wildcard.image, "oci", "//test/rbe/empty_worker:amd64_musl")
    collect("populated", "docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a", "firecracker", "//test/rbe/make_control:populated")
    if all(value["status"] == "OBSERVED" for value in row["arms"].values()):
        assert row["arms"]["scratch"]["compiler"] == row["arms"]["populated"]["compiler"]
        assert {key: value["check_sha256"] for key, value in row["arms"]["scratch"]["checks"].items()} == {key: value["check_sha256"] for key, value in row["arms"]["populated"]["checks"].items()}
        row["status"] = "PASS"
except Exception as err:
    row["error_type"] = type(err).__name__
finally:
    row["verified_at"] = wildcard.now()
    save()
print(row["status"], flush=True)
sys.exit(row["status"] != "PASS")
