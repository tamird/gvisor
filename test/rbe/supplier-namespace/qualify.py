"""Linux qualification for the wheel namespace producer in its two consumers."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time

stage = Path(os.environ["RUNNER_TEMP"]) / "namespace-results"
stage.mkdir(exist_ok=True)
supplier = "bfbcc014dd0be8763c467040eebd19baf926735b"
template = Path("python/private/pypi/namespace_pkg_tmpl.py")
patch = Path(__file__).with_name("namespace-initializers.patch")
bazel = shutil.which("bazelisk") or shutil.which("bazel")
assert bazel, "The Ubuntu worker must include Bazelisk."


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def bz(*args, lines=False):
    print("bazel", *args[:5], flush=True)
    if lines:
        return subprocess.check_output([bazel, *args], text=True).splitlines()
    subprocess.run([bazel, *args], check=True)


def inspect_output(path, expected):
    mode = path.lstat().st_mode
    assert stat.S_ISREG(mode), (path, oct(mode))
    data = path.read_bytes()
    assert data == expected, (path, data)
    return {"path": str(path), "mode": stat.filemode(mode), "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def files(targets, expected, arch=None):
    query = "set(" + " ".join(targets) + ")"
    options = ["--action_env=PATH=/no-executables"]
    if arch:
        options.append("--config=" + arch)
    bz("build", *options, *targets)
    aquery = json.loads("\n".join(bz("aquery", *options, "--output=jsonproto",
                                    'mnemonic("TemplateExpand",' + query + ')', lines=True)))
    actions = aquery.get("actions", [])
    assert len(actions) == len(targets), (targets, actions)
    output = bz("cquery", *options, "--output=files", query, lines=True)
    assert len(output) == len(targets), (targets, output)
    return [inspect_output(Path(path), expected) for path in output]


def supplier_cases(record):
    assert git("rev-parse", "HEAD") == supplier
    git("apply", "--check", str(patch))
    git("apply", str(patch))
    git("diff", "--check")
    expected = template.read_bytes()
    record["template_sha256"] = hashlib.sha256(expected).hexdigest()
    record["source"] = supplier
    fmt = Path(os.environ["RUNNER_TEMP"]) / "buildifier"
    subprocess.run([fmt, "-mode=check", "python/private/pypi/namespace_pkgs.bzl",
                    "python/private/pypi/BUILD.bazel",
                    "tests/pypi/namespace_pkgs/namespace_pkgs_tests.bzl"], check=True)
    suite = "//tests/pypi/namespace_pkgs:namespace_pkgs_tests"
    labels = bz("query", "tests(" + suite + ")", "--output=label", lines=True)
    assert len(labels) == 11 and any("create_inits" in x for x in labels), labels
    bep = Path(os.environ["RUNNER_TEMP"]) / "supplier-bep.json"
    bz("test", "--config=fast-tests", "--nocache_test_results", "--test_output=errors",
       "--build_event_json_file=" + str(bep), suite)
    records = [json.loads(x) for x in bep.read_text().splitlines()]
    actual = {r["id"]["testSummary"]["label"]: r["testSummary"]["overallStatus"]
              for r in records if "testSummary" in r}
    assert set(labels) == set(actual) and set(actual.values()) == {"PASSED"}, (labels, actual)
    record["tests"] = actual
    # Exercise the real upstream producer as well as its injectable analysis tests.
    fixture = Path("tests/pypi/namespace_pkgs/regular_file_control")
    fixture.mkdir()
    (fixture / "BUILD.bazel").write_text(
        'load("//python/private/pypi:namespace_pkgs.bzl", "create_inits")\n'
        'create_inits(srcs = ["alpha/beta/example.py"])\n')
    names = ["//" + str(fixture) + ":_cp_" + str(i) + "_namespace" for i in range(2)]
    record["generated"] = files(names, expected)


def downstream_cases(record):
    record["consumer_base"] = "d54eb3565cb4499a3d4051ebbef647ae0ccd410f"
    record["qualification_head"] = git("rev-parse", "HEAD")
    # This is the same qualified release source and module pin; replace the
    # release-only experiment's patch with the complete reviewed supplier diff.
    # Its injected producer has the same name/content contract on both versions.
    dest = Path("tools/rules_python_namespace_template.patch")
    assert dest.is_file()
    dest.write_bytes(patch.read_bytes())
    expected = (Path("..") / "supplier" / template).read_bytes()
    query = 'filter("_cp_[0-9]+_namespace$", deps(//tools/python_distribution:frontend))'
    names = sorted(set(bz("query", query, "--output=label", lines=True)))
    assert names, "no wheel initializer in the qualified downstream release frontend"
    assert all(name.startswith("@@") or name.startswith("@") for name in names), names
    record["wheel_rule_labels"] = names
    record["artifacts"] = {}
    for arch in ["x86_64", "aarch64"]:
        record["artifacts"][arch] = files(names, expected, arch)


def main():
    lane = sys.argv[1]
    record = {"lane": lane, "supplier": supplier, "status": "FAIL",
              "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest()}
    try:
        {"supplier": supplier_cases, "downstream": downstream_cases}[lane](record)
    except (Exception, SystemExit) as err:
        record["error"] = str(err)[:1300]
        raise
    else:
        record["status"] = "PASS"
    finally:
        record["verified_unix"] = time.time()
        (stage / (lane + ".json")).write_text(json.dumps(record, indent=2) + "\n")


if __name__ == "__main__":
    main()
