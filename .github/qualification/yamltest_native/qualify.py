"""Check original native Bazel TestResults for five YAML owners and controls."""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

BASE = "7eda82e34c64918d1f1b2a3d73a244d6979dbe44"
FIX = "dbd1c2e9f5dcae51de029ca00fcfd8bf4a52156c"
WORK = Path(os.environ["RUNNER_TEMP"]) / "yaml-native"
BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g"]
FLAGS = [
    "--config=x86_64", "--lockfile_mode=off", "--jobs=4", "--remote_executor=", "--remote_cache=",
    "--bes_backend=", "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
    "--define=yaml_native_proof=on",
]
REAL = [
    "//:nogo_config_test", "//:github_workflows_test", "//:buildkite_pipelines_test",
    "//website:cve_track_record_test", "//website:application_compatibility_test",
]
PKG = "test/yaml_native_proof"
ODD = "spaced $(YAMLTEST_LITERAL)'s input.yaml"
RESULT = {"baseline": BASE, "candidate": FIX, "expected_owners": REAL, "phases": {}}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write():
    (WORK / "result.json").write_text(json.dumps(RESULT, indent=2, sort_keys=True) + "\n")


def command(args):
    return subprocess.check_output(args, text=True).strip()


def fixture():
    root = Path(PKG)
    root.mkdir()
    (root / "schema.json").write_text('{"type":"object","required":["value"],"properties":{"value":{"type":"integer"}}}')
    (root / "unused.json").write_text('{"type":"boolean"}')
    (root / ODD).write_text("value: 1\nvalue: 2\n")
    (root / "comment.yaml").write_text("# a genuine YAML comment\nvalue: 3\n")
    (root / "wrong.yaml").write_text("value: wrong\n")
    fields = [
        'load("//tools/yamltest:defs.bzl", "yaml_test")',
        'config_setting(name = "active", define_values = {"yaml_native_proof": "on"})',
        'filegroup(name = "both", srcs = [%s, "comment.yaml"])' % json.dumps(ODD),
    ]
    controls = {
        "relaxed": ([':both'], False, False),
        "strict": ([ODD], True, False),
        "comment": (["comment.yaml"], False, True),
        "schema": (["wrong.yaml"], False, False),
        "empty": ([], False, False),
    }
    for name, (srcs, strict, comments) in controls.items():
        fields.append(
            'yaml_test(name = %r, srcs = select({":active": %s, "//conditions:default": ["wrong.yaml"]}), '
            'schema = select({":active": "schema.json", "//conditions:default": "unused.json"}), '
            'strict = select({":active": %r, "//conditions:default": %r}), '
            'disallow_comments = select({":active": %r, "//conditions:default": %r}))'
            % (name, json.dumps(srcs), strict, not strict, comments, not comments)
        )
    (root / "BUILD").write_text("\n".join(fields) + "\n")


def output(file):
    uri = urlsplit(file["uri"])
    assert uri.scheme == "file" and not uri.netloc
    data = Path(unquote(uri.path)).read_bytes()
    assert len(data) < 1024 * 1024
    digest = sha(data)
    assert file.get("digest", digest) == digest and int(file.get("length", len(data))) == len(data)
    return data


def observe(phase, expected, masked):
    bep = WORK / (phase + ".bep")
    console = WORK / (phase + ".log")
    args = [*BAZEL, "test", *FLAGS, "--nocache_test_results", "--flaky_test_attempts=1", "--keep_going",
            "--strategy=TestRunner=local", "--test_output=errors", "--build_event_json_file=" + str(bep),
            "--run_under=" + str(WORK / "probe.py"), "--test_env=YAMLTEST_MASK=" + str(int(masked)), *expected]
    with console.open("wb") as log:
        rc = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, check=False).returncode
    result = {"bazel_exit": rc, "rows": {}}
    RESULT["phases"][phase] = result
    write()
    if not bep.exists():
        result["diagnostic"] = console.read_text(errors="replace")[-1900:]
        write()
        raise ValueError("no original BEP")
    for line in bep.read_text().splitlines():
        event = json.loads(line)
        if "started" in event:
            result["invocation"] = event["started"]["uuid"]
        if "testResult" not in event:
            continue
        identity, test = event["id"]["testResult"], event["testResult"]
        label = identity["label"]
        assert label in expected and label not in result["rows"]
        outputs = {f["name"]: f for f in test["testActionOutput"]}
        data = output(outputs["test.log"])
        lines = data.decode(errors="replace").splitlines()
        markers = [json.loads(s.removeprefix("YAMLTEST_PROBE ")) for s in lines if s.startswith("YAMLTEST_PROBE ")]
        row = {
            "config": identity["configuration"]["id"], "status": test["status"],
            "strategy": test.get("executionInfo", {}).get("strategy"),
            "cached": bool(test.get("cachedLocally") or test.get("executionInfo", {}).get("cachedRemotely")),
            "attempt": int(identity.get("attempt", 1)), "log_sha256": sha(data), "log_bytes": len(data),
            "markers": markers, "validator_lines": [s[:550] for s in lines if any(k in s for k in
                ("✓", "already set", "YAML comments are disallowed", "Invalid type", "Usage of "))][:100],
        }
        if "test.xml" in outputs:
            row["xml_sha256"] = sha(output(outputs["test.xml"]))
        if not markers:
            row["diagnostic"] = lines[-10:]
        result["rows"][label] = row
    if set(result["rows"]) != set(expected):
        result["diagnostic"] = [s[:420] for s in console.read_text(errors="replace").splitlines()
                                if s.startswith("ERROR:")][:12]
    write()
    assert set(result["rows"]) == set(expected), (phase, set(expected) - set(result["rows"]))
    assert rc == (3 if any(expected.values()) else 0), (phase, rc)
    for label, row in result["rows"].items():
        assert row["status"] == ("FAILED" if expected[label] else "PASSED") and not row["cached"]
        assert row["attempt"] == 1 and row["strategy"] in ("local", "standalone")
        [marker] = row["markers"]
        assert marker["masked"] == masked and marker["exit"] == expected[label]
        if phase.startswith("stock"):
            assert marker["format"] == "#!/bin/bash" and (marker.get("exec_errno") == 2) == masked
        else:
            assert marker["format"] == "ELF" and marker["sha256"] == RESULT["validator_sha256"]
        if label in REAL and marker["exit"] == 0:
            assert any("✓" in s for s in row["validator_lines"])
    result["verified"] = True
    write()


def main():
    assert command(["git", "rev-parse", "HEAD^"]) == FIX
    assert command(["git", "rev-parse", "HEAD^^"]) == BASE
    assert command(["git", "diff", "--name-only", BASE, FIX]) == "tools/yamltest/defs.bzl"
    assert command(["git", "diff", "--name-only", FIX, "HEAD"]).splitlines() == [
        ".github/qualification/yamltest_native/probe.py", ".github/qualification/yamltest_native/qualify.py",
        ".github/workflows/build.yml",
    ]
    failures = {}
    def phase(name, f):
        try:
            f()
        except Exception as error:
            failures[name] = f"{type(error).__name__}: {error}"[:950]
            RESULT["failures"] = failures
            write()
    command(["git", "switch", "--detach", BASE])
    phase("stock_normal", lambda: observe("stock_normal", dict.fromkeys(REAL, 0), False))
    phase("stock_hidden", lambda: observe("stock_hidden", dict.fromkeys(REAL, 127), True))
    command(["git", "switch", "--detach", FIX])
    fixture()
    binary = command([*BAZEL, "cquery", *FLAGS, "--output=files", "//tools/yamltest:yamltest"]).splitlines()
    [path] = binary
    subprocess.run([*BAZEL, "build", *FLAGS, "//tools/yamltest:yamltest"], check=True)
    RESULT["validator_sha256"] = sha(Path(path).read_bytes())
    write()
    phase("fixed_hidden", lambda: observe("fixed_hidden", dict.fromkeys(REAL, 0), True))
    expected = {f"//{PKG}:{n}": code for n, code in {"relaxed": 0, "strict": 1, "comment": 1, "schema": 1, "empty": 2}.items()}
    phase("fixed_controls", lambda: observe("fixed_controls", expected, True))
    if "fixed_controls" not in failures:
        rows = RESULT["phases"]["fixed_controls"]["rows"]
        for n, text in {"relaxed": "✓", "strict": "already set", "comment": "YAML comments are disallowed",
                        "schema": "Invalid type", "empty": "Usage of "}.items():
            assert any(text in s for s in rows[f"//{PKG}:{n}"]["validator_lines"]), n
        relaxed = rows[f"//{PKG}:relaxed"]["markers"][0]["args"]
        assert relaxed == [f"-schema={PKG}/schema.json", "-strict=false", "-disallow_comments=false", "--",
                            f"{PKG}/{ODD}", f"{PKG}/comment.yaml"], relaxed
    assert not failures, failures
    RESULT["verified"] = True
    write()


if __name__ == "__main__":
    main()
