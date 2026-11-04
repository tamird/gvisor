"""Qualify public host-layout output and a modeled template boundary."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

import qualification_report as report


BASE = "71a996f3993d949dae24cc82fa396eb571d26087"
PACKAGES = ("", "attestation", "gasket", "ib", "linux", "linux/errno", "nvgpu", "sentry", "tpu")
LIBRARIES = ["//pkg/abi" + ("/" + p if p else "") + ":" + (p.split("/")[-1] if p else "abi") for p in PACKAGES]
TESTS = [p + "_hostlayout_test" for p in LIBRARIES]
GENERATED = [p + "_gen" for p in TESTS]
NOGO = [p + "_nogo" for p in LIBRARIES] + ["//tools/go_hostlayout:hostlayoutgen_nogo"]
MODEL = "//tools/go_hostlayout:qualification_private_output"
PREFIX = b"model.internal/"
ROOT = Path.cwd()
CARRIER = Path(__file__).resolve().parents[2]
PRODUCT, RC, DESTINATION = sys.argv[1:]
D = Path(DESTINATION)
D.mkdir()
BAZEL = ["bazel", "--bazelrc=" + RC]
R = {"source": PRODUCT, "baseline": BASE, "status": "INCOMPLETE", "phases": [],
     "scope": "Public Bazel contracts and modeled template replacement; no private integration execution"}


def now():
    return datetime.now(timezone.utc).isoformat()


def git(*args):
    return subprocess.check_output(["git", *args])


def digest(data):
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def save():
    payload = (json.dumps(R, indent=2, sort_keys=True) + "\n").encode()
    assert len(payload) < 400000, len(payload)
    assert not re.search(rb"x-buildbuddy-api-key|authorization\s*[:=]|github_pat_|gh[pousr]_", payload, re.I)
    (D / "result.json").write_bytes(payload)


def invoke(name, command, targets):
    phase = D / name
    phase.mkdir()
    row = {"name": name, "targets": targets, "status": "INCOMPLETE", "started_at": now()}
    R["phases"].append(row)
    args = [command, "--build_event_json_file=" + str(phase / "events.raw"),
            "--build_event_json_file_path_conversion=false", "--remote_download_outputs=toplevel"]
    if command == "test":
        args += ["--nocache_test_results", "--test_sharding_strategy=disabled", "--runs_per_test=1",
                 "--flaky_test_attempts=1", "--test_env=GO_TEST_WRAP_TESTV=1",
                 "--remote_download_regex=.*[/](test[.](log|xml))$"]
    args += targets
    (phase / "commit").write_bytes(git("cat-file", "commit", "HEAD"))
    (phase / "before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    with (phase / "command.log").open("wb") as log:
        code = subprocess.run(["timeout", "-k", "30", "1500", *BAZEL, *args], stdout=log, stderr=subprocess.STDOUT).returncode
    row.update(native_exit=code, execution_finished_at=now())
    raw = (phase / "command.log").read_bytes()
    row["console"] = digest(raw)
    row["invocations"] = re.findall(r"Invocation ID: ([0-9a-f-]{36})", raw.decode(errors="replace"))
    row["hints"] = [s for s in ("RESOURCE_EXHAUSTED", "no such target", "Permission denied", "signal: killed") if s.encode() in raw]
    row["status"] = "PASS" if code == 0 else "INCOMPLETE" if code in (124, 137) else "FAIL"
    save()
    return phase, row, args


def files(targets):
    # These are the exact configured output labels just built, not a wildcard.
    expression = "set(" + " ".join(targets) + ")"
    raw = subprocess.check_output(BAZEL + ["cquery", expression, "--output=starlark",
        "--starlark:expr=str(target.label) + '\\t' + '\\t'.join([f.path for f in target.files.to_list()])"], stderr=subprocess.DEVNULL, text=True)
    execroot = Path(subprocess.check_output(BAZEL + ["info", "execution_root"], stderr=subprocess.DEVNULL, text=True).strip()).resolve()
    result = {}
    for line in raw.splitlines():
        label, path = line.split("\t")
        if label.startswith("@@//"):
            label = label[2:]
        elif label.startswith("@//"):
            label = label[1:]
        assert label in targets and label not in result, label
        output = (execroot / path).resolve()
        assert output.is_relative_to(execroot) and output.is_file(), label
        result[label] = output.read_bytes()
    assert set(result) == set(targets), sorted(result)
    return result


def public_outputs(phase_name):
    phase, row, _ = invoke(phase_name, "build", GENERATED)
    assert row["native_exit"] == 0, row["native_exit"]
    events = [json.loads(line) for line in (phase / "events.raw").read_text().splitlines()]
    completed = {e["id"]["targetCompleted"]["label"] for e in events
                 if "targetCompleted" in e.get("id", {}) and e.get("completed", {}).get("success")}
    assert set(GENERATED) <= completed, sorted(completed)
    outputs = files(GENERATED)
    row["outputs"] = {label: digest(data) for label, data in outputs.items()}
    row["generated_bytes"] = sum(len(data) for data in outputs.values())
    save()
    return outputs


def model(phase_name, public, corrected):
    package = ROOT / "tools/go_hostlayout"
    build = package / "BUILD"
    original = build.read_bytes()
    fixture = CARRIER / ".github/qualification/hostlayout"
    template = package / "qualification_private_template.go"
    source = package / "qualification_attestation.go"
    assert not template.exists() and not source.exists()
    try:
        template.write_bytes((fixture / "private_template.go").read_bytes())
        source.write_bytes((ROOT / "pkg/abi/attestation/attestation.go").read_bytes())
        build.write_bytes(original + (fixture / "BUILD.fragment").read_bytes())
        _, row, _ = invoke(phase_name, "build", [MODEL])
        assert row["native_exit"] == 0, row["native_exit"]
        generated = files([MODEL])[MODEL]
        expected = public["//pkg/abi/attestation:attestation_hostlayout_test_gen"]
        operand = b'"gvisor.dev/gvisor/pkg/abi/attestation"'
        assert expected.count(operand) == 1
        copies = 1 if corrected else 2
        expected = expected.replace(operand, b'"' + PREFIX * copies + b'example.test/attestation"')
        row.update(generator_source=digest((package / "hostlayoutgen.go").read_bytes()),
                   model_template=digest(template.read_bytes()), input=digest(source.read_bytes()),
                   output=digest(generated), expected_prefix_copies=copies,
                   exact_expected_output=generated == expected)
        assert generated == expected, "modeled template output mismatch"
        row["status"] = "PASS" if corrected else "EXPECTED_BOUNDARY_FAILURE"
        save()
    finally:
        build.write_bytes(original)
        template.unlink(missing_ok=True)
        source.unlink(missing_ok=True)


def tests(generated):
    owners = TESTS + NOGO
    phase, row, args = invoke("public-tests", "test", owners)
    (phase / "after").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    (phase / "head").write_bytes(git("rev-parse", "HEAD"))
    (phase / "exit").write_text(str(row["native_exit"]) + "\n")
    (phase / "args").write_text("\n".join(args) + "\n")
    (phase / "expected").write_text("\n".join(owners) + "\n")
    context = {"expected_source": PRODUCT, "command_kind": "test", "execution_finished_at": row["execution_finished_at"],
               "commit_file": "commit", "source_before": "before", "source_after": "after", "final_head": "head",
               "native_exit": "exit", "capture_exit": "capture", "executed_arguments": "args"}
    (phase / "context").write_text(json.dumps(context))
    output_base = Path(subprocess.check_output(BAZEL + ["info", "output_base"], stderr=subprocess.DEVNULL, text=True).strip())
    clean = phase / "events.jsonl"
    captured = report.capture(phase / "events.raw", clean, output_base, phase / "reports")
    (phase / "capture").write_text(str(int(bool(captured["errors"]))) + "\n")
    verdict = report.verdict(clean, report.manifest_path(clean), phase / "context", phase / "expected")
    row["report_status"] = verdict["status"]
    row["report_problem_counts"] = {key: len(verdict.get(key, [])) for key in ("failures", "missing", "skips", "errors")}
    executions = []
    for line in clean.read_text().splitlines():
        event = json.loads(line)
        if "testResult" not in event:
            continue
        result = event["testResult"]
        info = result.get("executionInfo", {})
        executions.append({"identity": event["id"]["testResult"], "status": result["status"],
                           "remote": info.get("strategy") == "remote",
                           "cached": bool(result.get("cachedLocally") or info.get("cachedRemotely")),
                           "test_start": result.get("testAttemptStart"),
                           "test_duration": result.get("testAttemptDuration"),
                           "test_start_millis": result.get("testAttemptStartMillis"),
                           "test_duration_millis": result.get("testAttemptDurationMillis")})
    row["executions"] = executions
    finishes = []
    for execution in executions:
        start = execution["test_start"]
        duration = execution["test_duration"]
        if start and duration:
            began = datetime.fromisoformat(start.replace("Z", "+00:00"))
            elapsed = float(duration.removesuffix("s"))
        elif execution["test_start_millis"] is not None and execution["test_duration_millis"] is not None:
            began = datetime.fromtimestamp(int(execution["test_start_millis"]) / 1000, timezone.utc)
            elapsed = int(execution["test_duration_millis"]) / 1000
        else:
            continue
        finishes.append(began + timedelta(seconds=elapsed))
    row["test_timing_complete"] = bool(executions) and len(finishes) == len(executions)
    row["last_test_finished_at"] = max(finishes).isoformat() if finishes else None
    cases = []
    architecture_sections = {}
    row["reports"] = []
    for item in captured["reports"]:
        owner = item["identity"]["label"]
        row["reports"].append({k: item[k] for k in ("identity", "name", "sha256", "bytes")})
        path = phase / item["path"]
        if item["name"] == "test.xml":
            for case in ET.parse(path).iter("testcase"):
                outcome = "skipped" if case.get("status") == "notrun" or case.get("result") == "suppressed" else next((c.tag for c in case if c.tag in ("skipped", "failure", "error")), "pass")
                cases.append({"owner": owner, "name": case.get("name"), "result": outcome})
        if item["name"] == "test.log" and owner in NOGO:
            log = path.read_text(errors="replace")
            architecture_sections[owner] = {arch: bool(re.search(r"(?m)^" + arch + r"\.\.\.PASS\s*$", log)) for arch in ("amd64", "arm64")}
    row.update(cases=cases, architecture_sections=architecture_sections,
               hostlayout_cases=sum(c["owner"] in TESTS for c in cases))
    expected_cases = Counter((owner.removesuffix("_gen"), name.decode())
                             for owner, data in generated.items()
                             for name in re.findall(rb"(?m)^func (Test\w+)\(", data))
    observed_cases = Counter((c["owner"], c["name"]) for c in cases if c["owner"] in TESTS and c["result"] == "pass")
    row["generated_case_names_match"] = observed_cases == expected_cases
    save()
    assert row["native_exit"] == 0 and verdict["status"] == "PASS", row["report_status"]
    assert len(executions) == len(owners) and {e["identity"]["label"] for e in executions} == set(owners)
    assert all(e["remote"] and not e["cached"] and e["status"] == "PASSED" for e in executions)
    assert {c["owner"] for c in cases} == set(owners) and all(c["result"] == "pass" for c in cases)
    assert row["hostlayout_cases"] == 383, row["hostlayout_cases"]
    assert observed_cases == expected_cases and sum(expected_cases.values()) == 383
    assert set(architecture_sections) == set(NOGO) and all(all(a.values()) for a in architecture_sections.values())


try:
    assert re.fullmatch(r"[0-9a-f]{40}", PRODUCT) and PRODUCT != BASE
    assert git("rev-parse", "HEAD").decode().strip() == PRODUCT
    assert not git("status", "--porcelain", "--untracked-files=no")
    R["worker_image"] = os.environ["WORKER_IMAGE"]
    outputs = {}
    R["source_guard"] = {}
    for phase_name, source in (("baseline", BASE), ("candidate", PRODUCT)):
        subprocess.run(["git", "checkout", "--quiet", "--detach", source], check=True)
        defs = (ROOT / "tools/go_hostlayout/defs.bzl").read_bytes()
        boundary = (ROOT / "tools/defs.bzl").read_bytes()
        forbidden = b'"//tools/bazeldefs:go.bzl"'
        guard = forbidden not in defs and forbidden in boundary and b'"go_importpath"' in boundary
        R["source_guard"][phase_name] = {"adapter_load_at_boundary": guard,
                                         "hostlayout_defs": digest(defs), "tools_defs": digest(boundary)}
        assert guard == (phase_name == "candidate"), phase_name
        outputs[phase_name] = public_outputs(phase_name + "-generated")
        model(phase_name + "-model", outputs[phase_name], phase_name == "candidate")
    R["public_generated_equal"] = outputs["baseline"] == outputs["candidate"]
    assert R["public_generated_equal"], "public generated files changed"
    assert sum(map(len, outputs["candidate"].values())) == 94341
    tests(outputs["candidate"])
    R["status"] = "PASS"
except (AssertionError, KeyError, OSError, ValueError, ET.ParseError, subprocess.CalledProcessError) as exc:
    R["collector_error"] = type(exc).__name__
    if any(p.get("status") == "FAIL" or p.get("native_exit") == 3 for p in R["phases"]):
        R["status"] = "FAIL"
finally:
    R["verified_at"] = now()
    save()
sys.exit(int(R["status"] != "PASS"))
