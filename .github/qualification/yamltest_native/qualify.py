"""Check original native Bazel TestResults for five YAML owners and controls."""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

BASE = "7eda82e34c64918d1f1b2a3d73a244d6979dbe44"
FIX = "9ce2163af9df139378f2bd972b897e556895424b"
WORK = Path(os.environ["RUNNER_TEMP"]) / "yaml-native"
BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g"]
FLAGS = [
    "--config=x86_64", "--lockfile_mode=off", "--jobs=4", "--remote_executor=", "--remote_cache=",
    "--bes_backend=", "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
    "--define=yaml_native_proof=on",
]
# SHA-256 of sorted, newline-delimited paths reported by the original stock validator.
REAL = {
    "//:buildkite_pipelines_test": "b356e4658f8d7043c226a4f2bf3b9235b20722e5c57a332ca0a32e080f63437e",
    "//:github_workflows_test": "b2bde59edf0816f055ff7942772c352bd0ce63591da56c4f2fcab9a46a798c44",
    "//:nogo_config_test": "bf34a79d968a7e3b532d8b731f74c9c91e2ccee63d0323777c7c39e5192d9e83",
    "//website:application_compatibility_test": "96c250f21ba13adf56ed0f24be5b6a4c8f615d74f38bf7feb976d5b23f50725d",
    "//website:cve_track_record_test": "89515fdfe88d4842e922d4c2848d8fb316cdead50fa710368c0cda2167ff52ae",
}
PKG = "test/yaml_native_proof"
ODD = "spaced $(YAMLTEST_LITERAL)'s input.yaml"
SCHEMA = "schema $(YAMLTEST_LITERAL)'s spec.json"
RESULT = {"baseline": BASE, "candidate": FIX, "stock_receipt": 13595004667,
          "stock_zip_sha256": "b235b57d515051768180c5f8bb47ce1e8766da7c3108e3d1c59db83828431d02",
          "original_input_hashes": REAL, "phases": {}}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write():
    (WORK / "result.json").write_text(json.dumps(RESULT, indent=2, sort_keys=True) + "\n")


def command(args):
    return subprocess.check_output(args, text=True).strip()


def fixture():
    root = Path(PKG)
    root.mkdir()
    (root / SCHEMA).write_text('{"type":"object","required":["value"],"properties":{"value":{"type":"integer"}}}')
    (root / "unused.json").write_text('{"type":"boolean"}')
    (root / ODD).write_text("value: 1\nvalue: 2\n")
    (root / "comment.yaml").write_text("# a genuine YAML comment\nvalue: 3\n")
    (root / "wrong.yaml").write_text("value: wrong\n")
    fields = [
        'load("//tools/yamltest:defs.bzl", "yaml_test")',
        'config_setting(name = "active", define_values = {"yaml_native_proof": "on"})',
        'filegroup(name = "both", srcs = [%s, "comment.yaml"])' % json.dumps(ODD),
        'filegroup(name = "overlap", srcs = ["comment.yaml"])',
    ]
    controls = {
        "relaxed": ([":both", ":overlap"], False, False),
        "strict": ([ODD], True, False),
        "comment": (["comment.yaml"], False, True),
        "schema": (["wrong.yaml"], False, False),
        "empty": ([], False, False),
    }
    for name, (srcs, strict, comments) in controls.items():
        fields.append(
            'yaml_test(name = %r, srcs = select({":active": %s, "//conditions:default": ["wrong.yaml"]}), '
            'schema = select({":active": %s, "//conditions:default": "unused.json"}), '
            'strict = select({":active": %r, "//conditions:default": %r}), '
            'disallow_comments = select({":active": %r, "//conditions:default": %r}))'
            % (name, json.dumps(srcs), json.dumps(SCHEMA), strict, not strict, comments, not comments)
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
        assert marker["format"] == "ELF" and marker["sha256"] == RESULT["validator_sha256"]
        argv = marker["args"]
        assert argv[0] == "-schema" and argv[4] == "--" and len(argv) >= 6, argv
        if label in REAL:
            assert argv[1].endswith(("/schema.json", "/file/downloaded", ".schema.json")), argv
            comments = "true" if label == "//website:cve_track_record_test" else "false"
            assert argv[2:4] == ["-strict=true", "-disallow_comments=" + comments]
            paths = sorted(os.path.normpath(s[:-3]) for s in row["validator_lines"] if s.endswith(": ✓"))
            assert paths == sorted(os.path.normpath(name) for name in argv[5:])
            assert sha(("\n".join(paths) + "\n").encode()) == REAL[label], paths
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
    command(["git", "switch", "--detach", FIX])
    fixture()
    binary = command([*BAZEL, "cquery", *FLAGS, "--output=files", "//tools/yamltest:yamltest"]).splitlines()
    [path] = binary
    subprocess.run([*BAZEL, "build", *FLAGS, "//tools/yamltest:yamltest"], check=True)
    RESULT["validator_sha256"] = sha(Path(path).read_bytes())
    write()
    phase("fixed_hidden", lambda: observe("fixed_hidden", dict.fromkeys(REAL, 0), True))
    expected = {f"//{PKG}:{n}": code for n, code in {"relaxed": 0, "strict": 1, "comment": 1, "schema": 1}.items()}
    phase("fixed_controls", lambda: observe("fixed_controls", expected, True))
    phase("empty_sources", empty_sources)
    if "fixed_controls" not in failures:
        rows = RESULT["phases"]["fixed_controls"]["rows"]
        for n, text in {"relaxed": "✓", "strict": "already set", "comment": "YAML comments are disallowed",
                        "schema": "Invalid type"}.items():
            assert any(text in s for s in rows[f"//{PKG}:{n}"]["validator_lines"]), n
        relaxed = rows[f"//{PKG}:relaxed"]["markers"][0]["args"]
        assert relaxed == ["-schema", f"{PKG}/{SCHEMA}", "-strict=false", "-disallow_comments=false", "--",
                           f"{PKG}/comment.yaml", f"{PKG}/{ODD}"], relaxed
    assert not failures, failures
    RESULT["verified"] = True
    write()


def empty_sources():
    label = f"//{PKG}:empty"
    bep = WORK / "empty.bep"
    run = subprocess.run([*BAZEL, "test", *FLAGS, "--nobuild", "--build_event_json_file=" + str(bep), label],
                         text=True, capture_output=True, check=False)
    events = [json.loads(line) for line in bep.read_text().splitlines()]
    messages = [s[:600] for s in run.stderr.splitlines() if "expands to no files" in s]
    executed = [e for e in events if "testResult" in e]
    row = {"code": run.returncode, "label": label, "diagnostic": messages, "test_results": len(executed)}
    RESULT["phases"]["empty_sources"] = row
    write()
    assert run.returncode and not executed and any(f"//{PKG}:empty_srcs" in s for s in messages), row
    row["verified"] = True
    write()


if __name__ == "__main__":
    main()
