"""Check the original and native Nogo test on an AMD64 scratch executor."""

import collections
import datetime
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys

import qualification_report as report


SOURCE = os.environ["PRODUCT"]
IMAGE = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
VARIANT = os.environ["VARIANT"]
TARGETS = ["//pkg/waiter:waiter_nogo"]


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args])


def original_tests(tests, targets, wrapper="compiled_posix_test_wrapper"):
    if collections.Counter(t["target"] for t in tests) != collections.Counter(targets):
        return False
    for test in tests:
        package, name = test["target"].removeprefix("//").split(":")
        if (test["runner"] != "remote" or test["cache_hit"] or test["status"] or test["exit_code"]
                or Path(test["executable"]).name != wrapper
                or not test["test_binary"].startswith(package + "/")
                or Path(test["test_binary"]).name != name):
            return False
    return True


def native_actions(path, arch, tmp):
    """Reuse the qualified raw-spawn projection, retaining each test action."""
    # c37bd3473:test/rbe/empty_worker/wildcard.py; raw/expanded parity is
    # recorded in Actions 41282902598, artifact 13074631555.
    projection = 'select(has("spawn")) | .spawn | {m:.mnemonic,l:.targetLabel,x:.args[0],t:(if .mnemonic == "TestRunner" then .args[1:] else [] end),r:.runner,c:.cacheHit,s:.status,e:.exitCode,d:.digest,p:(.platform.properties // [])}'
    counts = collections.Counter()
    tests = []
    xml_actions = []
    bad = []
    with subprocess.Popen([str(tmp / "bb"), "print", "--raw", "--compact_execution_log=" + str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        with subprocess.Popen(["jq", "-c", projection], stdin=decoder.stdout,
                              stdout=subprocess.PIPE, text=True) as projected:
            decoder.stdout.close()
            for line in projected.stdout:
                row = json.loads(line)
                props = {p["name"]: p["value"] for p in row["p"]}
                platform = {key: props.get(key) for key in ("Arch", "container-image", "workload-isolation-type")}
                correct = platform == {"Arch": arch, "container-image": IMAGE, "workload-isolation-type": "oci"}
                runner = row["r"] or "unknown"
                counts["total"] += 1
                counts["off_platform"] += not correct
                counts["local"] += not row["c"] and runner != "remote"
                counts["failed"] += bool(row["s"] or row["e"])
                item = {"target": row["l"], "mnemonic": row["m"], "executable": row["x"],
                        "runner": runner, "cache_hit": bool(row["c"]), "platform": platform,
                        "status": row["s"], "exit_code": row["e"], "digest": row["d"]}
                if row["m"] == "TestRunner":
                    item["test_binary"] = row["t"][0]
                    item["test_args"] = row["t"][1:]
                    if item["test_binary"] == "--generate-xml":
                        xml_actions.append(item)
                    else:
                        tests.append(item)
                if (not correct or runner not in ("remote", "remote cache hit") or row["s"] or row["e"]) and len(bad) < 30:
                    bad.append(item)
            projection_exit = projected.wait()
        decode_exit = decoder.wait()
    valid_tests = original_tests(tests, TARGETS, "posix_tw" if arch == "amd64" else "compiled_posix_test_wrapper")
    return {"status": "PASS" if counts["total"] and not bad and not decode_exit and not projection_exit and valid_tests else "FAIL",
            "counts": dict(counts), "decoder_exits": [decode_exit, projection_exit],
            "tests": tests, "xml_actions": xml_actions, "failed_or_other": bad}


def output_evidence(bazel, common, work, execroot):
    with (work / "output-query.log").open("w") as log:
        result = subprocess.run([*bazel, "cquery", *common, "--output_groups=default",
                                 "--output=files", TARGETS[0]], stdout=subprocess.PIPE,
                                stderr=log, text=True, timeout=240)
    if result.returncode:
        raise ValueError("cquery did not return the test executable")
    files = [line for line in result.stdout.splitlines() if Path(line).name == "waiter_nogo"]
    if len(files) != 1:
        raise ValueError(f"Expected one test executable, found {files}")
    binary = execroot / files[0]
    with binary.open("rb") as f:
        header = f.read(16)
    kind = "bash" if header.startswith(b"#!/bin/bash\n") else "elf" if header.startswith(b"\x7fELF") else "other"
    outputs = {}
    for arch in ("amd64", "arm64"):
        path = binary.with_name(binary.name + "." + arch + ".findings")
        raw = path.read_bytes()
        entries = json.loads(raw) or []
        relevant = [entry for entry in entries if entry["Category"] == "checklocks"
                    and entry["GOARCH"] == arch and entry["Position"]["Filename"].endswith("waiter.go")]
        outputs[arch] = {"sha256": hashlib.sha256(raw).hexdigest(), "count": len(entries),
                         "waiter_lock_findings": relevant}
    return {"test_executable": kind, "reports": outputs}


def accepted_observation(row, code, text, capture):
    actions = row["actions"]
    observed = actions["tests"]
    if len(observed) != 1 or capture["errors"]:
        return False
    action = observed[0]
    original = original_tests([dict(action, status="", exit_code=0)], TARGETS, "posix_tw")
    counts = actions["counts"]
    if (not original or counts.get("local") or counts.get("off_platform")
            or any(actions["decoder_exits"])):
        return False
    if any(xml["target"] != action["target"] or xml["executable"] != action["executable"]
           or xml["runner"] != "remote" or xml["cache_hit"] or xml["exit_code"] or xml["status"]
           for xml in actions["xml_actions"]):
        return False
    outputs = row["outputs"]["reports"]
    kind = row["outputs"]["test_executable"]
    if VARIANT != "stock" and (action["test_args"][:3] != ["filter", "-test", "-text"]
                               or sorted(action["test_args"][3:]) != ["nogo.amd64.findings", "nogo.arm64.findings"]):
        return False
    if VARIANT == "clean":
        return (not code and row["tests_status"] == "PASS" and actions["status"] == "PASS"
                and kind == "elf" and all(not v["count"] for v in outputs.values())
                and "PASS" in text.splitlines())
    if not (code == 3 and (action["exit_code"] or action["status"])
            and counts.get("failed") == 1 and row["tests_status"] == "FAIL"):
        return False
    if VARIANT == "stock":
        return (kind == "bash" and all(not v["count"] for v in outputs.values())
                and re.search(r"no such file|not found|ENOENT", text, re.I) is not None)
    return (kind == "elf" and "checklocks" in text
            and all(v["count"] == len(v["waiter_lock_findings"]) > 0 and arch in text
                    for arch, v in outputs.items()))


def qualify(arch, tmp, out, private, bazel):
    work = private / arch
    work.mkdir()
    public = out / arch
    public.mkdir()
    row = {"source": SOURCE, "arch": arch, "targets": TARGETS, "experiment": VARIANT, "phases": {}, "status": "INCOMPLETE"}
    write(public / "receipt.json", row)
    (work / "commit").write_bytes(git("cat-file", "commit", SOURCE))
    (work / "before").write_bytes(git("diff", "HEAD", "--"))
    (work / "expected.txt").write_text("\n".join(TARGETS) + "\n")
    platform = "//test/rbe/empty_worker:" + arch + "_musl"
    common = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch],
              "--compilation_mode=fastbuild", "--host_platform=" + platform,
              "--extra_execution_platforms=" + platform,
              "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
              "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
              "--noincompatible_exclude_starlark_flags_from_exec_config",
              "--workspace_status_command=", "--features=fully_static_link", "--build_tag_filters=",
              "--aspects=//tools/nogo:defs.bzl%nogo_aspect_tricorder", "--output_groups=+tricorder",
              "--incompatible_use_native_posix_test_wrapper", "--nocache_test_results",
              "--nobuild_event_json_file_path_conversion", "--jobs=400",
              # The test executable is a symlink to the target-configured Go binary.
              "--remote_download_regex=.*/bin/(tools/nogo/nogo_/nogo|pkg/waiter/waiter_nogo(\\.(amd64|arm64)\\.findings)?)$",
              "--remote_download_outputs=toplevel", "--noremote_upload_local_results",
              "--noremote_local_fallback", "--spawn_strategy=remote", "--keep_going",
              "--color=no", "--curses=no"]
    extra = []
    for phase in ("analysis", "test"):
        raw = work / (phase + ".bep")
        flags = ["--nobuild"] if phase == "analysis" else ["--execution_log_compact_file=" + str(work / "actions.compact")]
        args = ["test", *common, *extra, *flags, "--build_event_json_file=" + str(raw), *TARGETS]
        started = now()
        with (work / (phase + ".log")).open("w") as log:
            code = subprocess.run(["timeout", "--signal=INT", "--kill-after=25s", "12m" if phase == "analysis" else "43m",
                                   *bazel, *args], stdout=log, stderr=subprocess.STDOUT).returncode
        finished = now()
        phase_log = (work / (phase + ".log")).read_text(errors="replace")
        invocation_ids = sorted(set(re.findall(r"https://app\.buildbuddy\.io/invocation/([0-9a-f-]{36})", phase_log)))
        diagnostics = [line[:1000] for line in phase_log.splitlines()
                       if re.search(r"error:|no such file|not found|permission denied|exec format|cannot execute|bad substitution|syntax error|go: |fork/exec", line, re.I)
                       and not re.search(r"api.key|remote.header|bes.header|authorization", line, re.I)][:12]
        row["phases"][phase] = {"started_at": started, "execution_finished_at": finished, "native_exit": code,
                                "invocation_ids": invocation_ids, "diagnostic_lines": diagnostics if code else []}
        write(public / "receipt.json", row)
        if phase == "analysis":
            preflight = report.preflight(raw, code, "test")
            write(public / "analysis.json", preflight)
            if preflight["status"] != "PASS":
                return row
            extra = preflight["output_options"]
            continue
        (work / "arguments").write_text("\n".join(args) + "\n")
        (work / "native_exit").write_text(str(code))
        (work / "after").write_bytes(git("diff", "HEAD", "--"))
        (work / "head").write_bytes(git("rev-parse", "HEAD"))
        execroot = Path(subprocess.check_output([*bazel, "info", "execution_root"], stderr=subprocess.DEVNULL, text=True).strip())
        sanitized = work / "sanitized.bep"
        capture = report.capture(raw, sanitized, execroot, work / "reports", False, (work / "analysis.bep",), "test")
        (work / "capture_exit").write_text(str(int(bool(capture["errors"]))))
        context = {"expected_source": SOURCE, "commit_file": "commit", "source_before": "before",
                   "source_after": "after", "final_head": "head", "executed_arguments": "arguments",
                   "native_exit": "native_exit", "capture_exit": "capture_exit",
                   "execution_finished_at": finished, "command_kind": "test"}
        write(work / "context.json", context)
        verdict = report.verdict(sanitized, report.manifest_path(sanitized), work / "context.json",
                                 work / "expected.txt", (work / "analysis.bep",))
        write(public / "tests.json", verdict)
        for xml in (work / "reports").rglob("test.xml"):
            relative = xml.relative_to(work / "reports")
            destination = public / "xml" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(xml, destination)
        row["tests_status"] = verdict["status"]
        row["status"] = verdict["status"]
        logs = list((work / "reports").rglob("test.log"))
        text = "\n".join(path.read_text(errors="replace") for path in logs)
        row["test_log"] = [line[:1200] for line in text.splitlines()
                           if re.search(r"PASS|checklocks|ENOENT|not found|no such file", line, re.I)
                           and not re.search(r"header|token|api.key|authorization", line, re.I)][:30]
        # A missing downloaded input must not discard the original filtered log.
        write(public / "receipt.json", row)
        row["actions"] = native_actions(work / "actions.compact", arch, tmp)
        write(public / "receipt.json", row)
        row["outputs"] = output_evidence(bazel, common, work, execroot)
        row["verified_at"] = now()
        row["status"] = "PASS" if accepted_observation(row, code, text, capture) else "FAIL"
        write(public / "receipt.json", row)
    return row


def main():
    tmp = Path(os.environ["RUNNER_TEMP"])
    out = tmp / "native-package-evidence" / VARIANT
    out.mkdir(exist_ok=True)
    private = tmp / ("native-package-private-" + VARIANT)
    private.mkdir(exist_ok=True)
    control = json.loads((tmp / "native-wrapper-control.json").read_text())
    actual = control["test_action"]
    targets = [actual["target"]]
    if not original_tests([actual], targets):
        raise ValueError("The verifier rejects the retained successful native TestRunner")
    if original_tests([dict(actual, executable="external/bazel_tools/tools/test/test-setup.sh")], targets):
        raise ValueError("The verifier accepts a shell TestRunner as native")
    write(out / "wrapper-control.json", {"status": "PASS", "provenance": control["provenance"]})
    try:
        subprocess.run(["git", "checkout", "--detach", SOURCE], check=True, stdout=subprocess.DEVNULL)
        if git("rev-parse", "HEAD").decode().strip() != SOURCE or git("diff", "HEAD", "--").strip():
            raise ValueError("Product source does not match the qualified commit")
        binary = tmp / "bazel-candidate"
        binary_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
    except (subprocess.CalledProcessError, OSError, ValueError) as err:
        write(out / "summary.json", {"source": SOURCE, "status": "INCOMPLETE",
                                    "phase": "source_and_supplier_inputs", "executed_targets": 0,
                                    "collection_error": str(err), "verified_at": now()})
        return 1
    bazel = [str(binary), "--bazelrc=" + str(tmp / "sidecar.bazelrc")]
    rows = []
    for arch in ("amd64",):
        try:
            rows.append(qualify(arch, tmp, out, private, bazel))
        except Exception as err:
            # Retain any already-written original result, including test failure.
            path = out / arch / "receipt.json"
            row = json.loads(path.read_text()) if path.exists() else {"arch": arch, "status": "INCOMPLETE"}
            if row.get("phases", {}).get("test", {}).get("native_exit", 0):
                row["status"] = "FAIL"
            elif row.get("status") != "FAIL":
                row["status"] = "INCOMPLETE"
            row["collection_error"] = type(err).__name__ + ": " + str(err)[:1400]
            row["verified_at"] = now()
            write(path, row)
            rows.append(row)
    status = "FAIL" if any(row["status"] == "FAIL" for row in rows) else "PASS" if all(row["status"] == "PASS" for row in rows) else "INCOMPLETE"
    summary = {"source": SOURCE, "status": status, "rows": rows, "verified_at": now(),
               "native_bazel": {"supplier_source": "c2f5f79dce24877679cede14b85666b7ca65c6de",
                                "member": "bazel-candidate",
                                "sha256": binary_sha256,
                                "identity": "Canonical supplier build; static embedded ELF verified in supplier.json",
                                "wrapper_prerequisite": "https://github.com/bazelbuild/bazel/pull/30837",
                                "prior_native_xml_proof": "https://github.com/tamird/gvisor/actions/runs/41071795282/artifacts/12986015991"}}
    write(out / "summary.json", summary)
    return int(summary["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
