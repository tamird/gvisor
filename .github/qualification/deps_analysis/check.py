"""Compare the current policy on stock, standard and a native result reader."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

import previous


BASE = "975fb172241c7946a9f67b4f52e0349d9240589a"
STANDARD = "ebb314fc9f3d09da06f20c5bd601eeb1cf861f80"
WORK = Path(os.environ["RUNNER_TEMP"]) / "deps-check"
OUT = WORK / "receipt"
PKG = Path("tools/deps_test_fixture")
PREFIX = "//" + str(PKG) + ":"
FAIL = PREFIX + "forbidden_test"
REAL = ["//pkg/tcpip:netstack_deps_test", "//runsc:runsc_deps_test", "//runsc/cmd/sentry:gvisor_sentry_deps_test"]
ALL = [*REAL, FAIL]
RULE = Path("tools/deps.bzl")
RC = WORK / "inputs.bazelrc"
BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(RC)]
FLAGS = ["--remote_executor=", "--remote_cache=", "--bes_backend=", "--lockfile_mode=off", "--jobs=4"]
# Cross-analysis and building an executable do not require a native ARM TestRunner.
ARM_BUILD = ["--config=aarch64", "--@bazel_tools//tools/test:incompatible_use_default_test_toolchain=false"]
RESULT = {"base": BASE, "standard": STANDARD, "verified": False, "phases": {}, "errors": {}}
END = time.monotonic() + 62 * 60


def save():
    (OUT / "result.json").write_text(json.dumps(RESULT, indent=2) + "\n")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args])


def label(value):
    return value[value.index("//"):]


def bazel(name, args, allowed=(0,)):
    for attempt in range(12):
        RC.write_text("".join("common --distdir=" + str(d) + "\n" for d in sorted(previous.DIRS)))
        p = subprocess.run(BAZEL + args, capture_output=True, timeout=max(1, END - time.monotonic()))
        text = p.stderr.decode(errors="replace")
        row = {"rc": p.returncode, "seconds_left": round(END - time.monotonic()),
               "errors": [line[:800] for line in text.splitlines() if "ERROR:" in line][-14:]}
        RESULT.setdefault("commands", {}).setdefault(name, []).append(row)
        save()
        if p.returncode in allowed:
            return p
        # A failed real TestRunner already has its own BEP evidence.
        if p.returncode == 3 or not previous.recover(text):
            raise RuntimeError((name, row, text[-880:]))
    raise RuntimeError("exhausted public input preparation for " + name)


def read_output(entry):
    url = urlsplit(entry["uri"])
    assert url.scheme == "file" and not url.netloc
    data = Path(unquote(url.path)).read_bytes()
    assert 0 < len(data) < (1 << 20)
    assert entry.get("digest", sha(data)) == sha(data)
    assert int(entry.get("length", len(data))) == len(data)
    return data


def diagnostics(text):
    return [label(s.lstrip("\t")) for s in text.splitlines() if (str(PKG) + ":") in s
            and (s.startswith("\t") or s.endswith(" depends on:"))]


def test(source, mode, owners):
    phase = source + "-" + mode
    bep = WORK / (phase + ".bep")
    rows = RESULT["phases"][phase] = {}
    expected_rc = {"stock": 127, "standard": 127, "native": 0}
    # A non-test Bazel failure may still leave a valid result for another owner.
    p = bazel(phase, ["test", *FLAGS, "--config=x86_64", "--nocache_test_results",
        "--strategy=TestRunner=local", "--flaky_test_attempts=1", "--keep_going",
        "--run_under=" + str(WORK / "probe.py"), "--test_env=TEST_DEPS_MODE=" + mode,
        "--build_event_json_file=" + str(bep), *owners], allowed=(0, 1, 3))
    completed = {}
    for raw in bep.read_bytes().splitlines():
        event = json.loads(raw)
        identity = event["id"]
        if "targetCompleted" in identity and "completed" in event:
            target = identity["targetCompleted"]
            if target["label"] in owners and "aspect" not in target:
                completed[target["label"]] = target["configuration"]["id"]
        if "testResult" not in event:
            continue
        target, result = identity["testResult"], event["testResult"]
        owner = target["label"]
        assert owner in owners and owner not in rows
        stem = phase + "-" + owner.split(":")[-1]
        outputs = {f["name"]: f for f in result["testActionOutput"]}
        data = {}
        for filename, extension in (("test.log", "log"), ("test.xml", "xml")):
            data[extension] = read_output(outputs[filename])
            (OUT / (stem + "." + extension)).write_bytes(data[extension])
        text = data["log"].decode(errors="replace")
        marks = [json.loads(line.removeprefix("DEPS_TEST_PROBE ")) for line in text.splitlines()
                 if line.startswith("DEPS_TEST_PROBE ")]
        rows[owner] = {"configuration": target["configuration"]["id"], "status": result["status"],
                       "attempt": int(target.get("attempt", 1)),
                       "cached": bool(result.get("cachedLocally") or result.get("executionInfo", {}).get("cachedRemotely")),
                       "strategy": result.get("executionInfo", {}).get("strategy"), "probe": marks,
                       "log_sha256": sha(data["log"]), "xml_sha256": sha(data["xml"]),
                       "policy_header": "Invalid dependencies found. If you're sure you want to add dependencies,\nmodify this target." in text,
                       "diagnostics": diagnostics(text)}
        ElementTree.fromstring(data["xml"])
        save()
    assert set(rows) == set(owners), (phase, p.returncode, set(owners) - set(rows))
    assert p.returncode == 3  # Every invocation includes the deliberate forbidden edge.
    for owner, row in rows.items():
        [probe] = row["probe"]
        assert row["configuration"] == completed[owner] and row["configuration"]
        assert row["attempt"] == 1 and not row["cached"] and row["strategy"] in ("local", "standalone")
        rc = (expected_rc[source] if mode == "empty" and source != "native" else int(owner == FAIL))
        assert row["status"] == ("PASSED" if rc == 0 else "FAILED") and probe["exit"] == rc
        assert probe["format"] == {"stock": "#!/bin/bash", "standard": "#!/bin/sh", "native": "ELF"}[source]
        assert (probe.get("exec_errno") == 2) == (rc == 127)
        assert row["policy_header"] == (rc == 1)
        expected = []
        if rc == 1:
            expected = [PREFIX + "source depends on:", PREFIX + "ordinary", PREFIX + ("price" if source == "stock" else "price$usd")]
        assert row["diagnostics"] == expected, (phase, owner, row["diagnostics"], expected)


def configured(source, arch):
    phase = source + "-graph-" + arch
    platform = ARM_BUILD if arch == "aarch64" else ["--config=x86_64"]
    result = bazel(phase, ["cquery", *FLAGS, *platform, "set(" + " ".join(ALL) + ")",
                          "--output=starlark", "--starlark:file=" + str(WORK / "configured.bzl")])
    rows = {}
    for line in result.stdout.splitlines():
        row = json.loads(line)
        rows[label(row["label"])] = row
    RESULT["phases"][phase] = rows
    save()
    assert set(rows) == set(ALL)
    assert all(len(row["default_files"]) == 1 for row in rows.values())
    assert all(("linux_arm64" if arch == "aarch64" else "linux_amd64") in row["platform"] for row in rows.values())
    if source == "standard":
        ok, message = rows[FAIL]["analysis_result"]
        assert not ok and "price$usd" in message and ("arm_only" in message) == (arch == "aarch64")
        assert all(rows[owner]["analysis_result"] == [True, ""] for owner in REAL)
    for name in ("source.validation", "allowed_separately.validation"):
        assert any(path.endswith("/" + name) for path in rows[FAIL]["validation"]) == (source != "standard")
    return rows


def once(name, fn):
    try:
        fn()
    except Exception as error:
        RESULT["errors"][name] = repr(error)[:3800]
    save()


def fixture():
    PKG.mkdir()
    for src, dst in (("fixture.BUILD", "BUILD"), ("fixture.bzl", "fixture.bzl"), ("reader.go", "reader.go")):
        shutil.copyfile(WORK / src, PKG / dst)


def native_control(standard):
    result = standard
    for old, new in (
        ("def _deps_test_impl(ctx):", (WORK / "native_result.bzl").read_text() + "\ndef _deps_test_impl(ctx):"),
        ('return [AnalysisTestResultInfo(success = True, message = "")]', 'return _native_result(ctx, True, "")'),
        ('return [AnalysisTestResultInfo(success = False, message = "\\n".join(lines))]',
         'return _native_result(ctx, False, "\\n".join(lines))'),
        ('        "targets": attr.label_list(', '        "_reader": attr.label(default = "//tools/deps_test_fixture:reader", executable = True, cfg = "target"),\n        "targets": attr.label_list('),
        ("    analysis_test = True,", "    test = True,"),
    ):
        assert result.count(old) == 1, old
        result = result.replace(old, new)
    return result


def arm_default():
    rows = RESULT["phases"]["native-graph-aarch64"]
    owner = REAL[0]
    bazel("native-arm-build", ["build", *FLAGS, *ARM_BUILD, owner])
    path, = rows[owner]["default_files"]
    raw = Path(path).read_bytes()
    machine = int.from_bytes(raw[18:20], "little")
    RESULT["arm_default"] = {"label": owner, "path": path, "sha256": sha(raw), "bytes": len(raw), "elf_machine": machine}
    assert raw[:6] == b"\x7fELF\x02\x01" and machine == 183


def selection_and_validation(source):
    query = "kind('deps_test rule', tests(//pkg/tcpip:all + //runsc:all + //runsc/cmd/sentry:all))"
    selected = bazel(source + "-selection", ["query", "--lockfile_mode=off", query])
    labels = sorted(label(line) for line in selected.stdout.decode().splitlines())
    RESULT.setdefault("wildcard_selection", {})[source] = labels
    assert labels == sorted(REAL), labels
    name = source + "-invalid-validation"
    bep = WORK / (name + ".bep")
    p = bazel(name, ["build", *FLAGS, "--config=x86_64", "--output_groups=_validation",
              "--build_event_publish_all_actions", "--build_event_json_file=" + str(bep),
              PREFIX + "validation_test"], allowed=(0, 1))
    events = [json.loads(line) for line in bep.read_bytes().splitlines()]
    failed = [event["id"]["actionCompleted"] for event in events if "action" in event
              and event["action"].get("type") == "InvalidDepsTestFixture" and not event["action"].get("success")]
    RESULT.setdefault("invalid_validations", {})[source] = {"exit": p.returncode, "failed_actions": failed,
                                                           "diagnostic": b"forbidden fixture validation reached" in p.stderr}
    save()
    assert p.returncode == (0 if source == "standard" else 1)
    assert len(failed) == (0 if source == "standard" else 1)
    if failed:
        assert failed[0]["label"] == PREFIX + "invalid_validation" and failed[0]["primaryOutput"].endswith("invalid_validation.validation")
        assert b"forbidden fixture validation reached" in p.stderr


def main():
    assert git("rev-parse", "HEAD").decode().strip() == STANDARD
    assert git("rev-parse", "HEAD^").decode().strip() == BASE
    assert git("diff", "--name-only", BASE, STANDARD).splitlines() == [b"tools/deps.bzl"]
    fixture()
    previous.OUT = WORK
    previous.REPORT["assets"] = RESULT.setdefault("public_assets", {})
    standard = git("show", STANDARD + ":tools/deps.bzl").decode()
    for source, content in (("stock", git("show", BASE + ":tools/deps.bzl").decode()),
                            ("standard", standard), ("native", native_control(standard))):
        RULE.write_text(content)
        RESULT.setdefault("rule_sha256", {})[source] = sha(RULE.read_bytes())
        once(source + "/validation", lambda: selection_and_validation(source))
        for arch in ("x86_64", "aarch64"):
            once(source + "/" + arch, lambda: configured(source, arch))
        modes = {"stock": ["host", "empty"], "standard": ["host", "sh-only", "empty"], "native": ["empty"]}[source]
        for mode in modes:
            owners = ALL if mode == "host" or source == "native" else [REAL[0], FAIL]
            once(source + "/" + mode, lambda: test(source, mode, owners))
        if source == "native":
            once("native/arm-default", arm_default)
    if not RESULT["errors"]:
        for arch in ("x86_64", "aarch64"):
            stock = RESULT["phases"]["stock-graph-" + arch]
            native = RESULT["phases"]["native-graph-" + arch]
            assert all(stock[owner]["validation"] == native[owner]["validation"] for owner in ALL)
        rows = RESULT["phases"]["native-empty"]
        assert len({row["probe"][0]["sha256"] for row in rows.values()}) == 1
        RESULT["verified"] = True
    save()
    return 0 if RESULT["verified"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        save()
