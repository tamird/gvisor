"""Compare the ordinary GNU syscall entry point and its existing native launcher."""

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
from static_build import elf


SOURCE = os.environ["PRODUCT"]
STOCK = "851d04fc2918adcaa60a78fd9fa7e766bc4fec68"
IMAGE = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
TARGETS = ["//test/syscalls:fork_test_native"]
CASES = {TARGETS[0]: ["ForkTest/Simple", "ForkTest/Pipe", "ForkTest/SharedMapping"]}
ABI = {"selected": "GNU target platform; musl only for execution tools",
       "build_proof": {"run": 41300981826, "artifact": 13081601435},
       "launcher_proposal": "8919cb68b11643114c1f28c1a756902a2958bda0",
       "scope": "Ordinary native entry point, no run_under or target static-link override"}


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


def native_actions(path, arch, tmp, variant):
    """Reuse the qualified raw-spawn projection, retaining each test action."""
    # c37bd3473:test/rbe/empty_worker/wildcard.py; raw/expanded parity is
    # recorded in Actions 41282902598, artifact 13074631555.
    projection = 'select(has("spawn")) | .spawn | {m:.mnemonic,l:.targetLabel,x:.args[0],t:(if .mnemonic == "TestRunner" then .args[1:] else null end),r:.runner,c:.cacheHit,s:.status,e:.exitCode,d:.digest,p:(.platform.properties // []),v:[.envVars[]? | select(.name == "GVISOR_SYSCALL_TEST_BINARY" or .name == "GVISOR_SYSCALL_TEST_RUNNER")]}'
    counts = collections.Counter()
    tests = []
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
                    item["arguments"] = row["t"]
                    item["runfile_environment"] = {value["name"]: value["value"] for value in row["v"]}
                    item["test_binary"] = row["t"][0] if row["t"] else ""
                    tests.append(item)
                if (not correct or runner not in ("remote", "remote cache hit") or (row["m"] != "TestRunner" and (row["s"] or row["e"]))) and len(bad) < 30:
                    bad.append(item)
            projection_exit = projected.wait()
        decode_exit = decoder.wait()
    original = [test for test in tests if test["test_binary"] != "--generate-xml"]
    auxiliary = [test for test in tests if test["test_binary"] == "--generate-xml"]
    valid_tests = collections.Counter(test["target"] for test in original) == collections.Counter(TARGETS)
    wrapper = "posix_tw" if arch == "amd64" else "compiled_posix_test_wrapper"
    for test in original:
        arguments = test["arguments"]
        valid_tests &= (test["runner"] == "remote" and not test["cache_hit"]
                        and Path(test["executable"]).name == wrapper
                        and test["test_binary"] == "test/syscalls/fork_test_native")
        if variant == "stock":
            valid_tests &= arguments == ["test/syscalls/fork_test_native"]
        else:
            valid_tests &= (arguments[1:2] == ["--syscall-test"] and "--platform=native" in arguments
                            and test["runfile_environment"] == {
                                "GVISOR_SYSCALL_TEST_BINARY": "test/syscalls/linux/fork_test",
                                "GVISOR_SYSCALL_TEST_RUNNER": "test/runner/runner_/runner"})
    return {"status": "PASS" if counts["total"] and not bad and not decode_exit and not projection_exit and valid_tests else "INCOMPLETE",
            "counts": dict(counts), "decoder_exits": [decode_exit, projection_exit],
            "tests": original, "xml_actions": auxiliary, "failed_or_other": bad}


def product_files(bazel, common, work, execroot, arch, variant):
    targets = [*TARGETS, "//test/syscalls/linux:fork_test"]
    if variant != "stock":
        targets.append("//test/runner/setup_container:setup_container")
    with (work / "output-query.log").open("w") as log:
        result = subprocess.run(["timeout", "--signal=INT", "--kill-after=25s", "8m",
                                 *bazel, "cquery", *common, "--output=files", "set(" + " ".join(targets) + ")"],
                                stdout=subprocess.PIPE, stderr=log, text=True)
    if result.returncode:
        return {"status": "INCOMPLETE", "native_exit": result.returncode}
    outputs = {}
    for target in targets:
        name = target.split(":")[1]
        files = [line for line in result.stdout.splitlines() if Path(line).name == name]
        if len(files) != 1 or not (execroot / files[0]).is_file():
            return {"status": "INCOMPLETE", "outputs": outputs,
                    "reason": "The original executable was not materialized locally", "target": target}
        content = (execroot / files[0]).read_bytes()
        data = {"path": files[0], "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        if variant == "stock" and target == TARGETS[0] and not content.startswith(b"#!/bin/bash\n"):
            return {"status": "FAIL", "outputs": outputs, "reason": "Stock native target is not the expected Bash script"}
        if content.startswith(b"#!"):
            data.update(kind="script", interpreter=content.splitlines()[0].decode().removeprefix("#!"))
            data["status"] = "PASS" if variant == "stock" and target == TARGETS[0] and data["interpreter"] == "/bin/bash" else "FAIL"
        else:
            data.update(kind="ELF", **elf(content))
            data["status"] = "PASS" if data["machine"] == {"amd64": 62, "arm64": 183}[arch] else "FAIL"
        outputs[target] = data
    if variant != "stock":
        if outputs[TARGETS[0]]["sha256"] != outputs[targets[-1]]["sha256"]:
            return {"status": "FAIL", "outputs": outputs, "reason": "The native launcher differs from setup_container"}
    return {"status": "PASS" if all(v["status"] == "PASS" for v in outputs.values()) else "FAIL", "outputs": outputs}


def failure_lines(reports):
    lines = []
    for path in reports.rglob("test.log"):
        for line in path.read_text(errors="replace").splitlines():
            if (re.search(r"execv[pel]*:|execv[pel]*\(|no such file|permission denied|exec format|cannot execute|fork/exec|^\[ *(RUN|FAILED|PASSED)", line, re.I)
                    and not re.search(r"api.key|remote.header|bes.header|authorization|token=|credential", line, re.I)):
                lines.append(line[:1200])
    return lines[:30]


def observation(row, verdict):
    """Separate a qualified runtime from an evidenced failed exec boundary."""
    if row["actions"]["status"] != "PASS" or row["original_files"]["status"] != "PASS":
        return {"status": "INCOMPLETE", "cause": "Missing action or original executable evidence"}
    if verdict["status"] == "PASS":
        return {"status": "PASS", "cause": "All required syscall cases passed"}
    originals = row["actions"]["tests"]
    failed = verdict.get("executed", [])
    matching_error = [line for line in row["original_log_diagnostics"]
                      if re.fullmatch(r"ERROR: could not execute (?:.*/)?test/syscalls/fork_test_native: No such file or directory", line)]
    if (verdict.get("native_exit") == 3 and verdict.get("bep_finish", {}).get("exitCode", {}).get("code") == 3
            and len(originals) == 1 and originals[0]["exit_code"] == 127
            and len(failed) == 1 and failed[0].get("owner", {}).get("label") == TARGETS[0]
            and failed[0].get("status") == "FAILED" and matching_error):
        return {"status": "PASS", "cause": "The native wrapper's exec of the original launcher returned ENOENT",
                "diagnostic": matching_error,
                "declared_interpreter": row["original_files"]["outputs"][TARGETS[0]]["interpreter"],
                "inference": "The existing launcher and its interpreter declaration distinguish this boundary from later runner or namespace failures"}
    return {"status": "INCOMPLETE", "cause": "Failure is preserved; the expected launch boundary is not established"}


def observation_controls():
    row = {"actions": {"status": "PASS", "tests": [{"exit_code": 127}]},
           "original_files": {"status": "PASS", "outputs": {TARGETS[0]: {"interpreter": "/bin/bash"}}},
           "original_log_diagnostics": ["ERROR: could not execute test/syscalls/fork_test_native: No such file or directory"]}
    verdict = {"status": "FAIL", "native_exit": 3, "bep_finish": {"exitCode": {"code": 3}},
               "executed": [{"owner": {"label": TARGETS[0]}, "status": "FAILED"}]}
    if observation(row, verdict)["status"] != "PASS":
        raise ValueError("The launch-failure predicate rejects its complete witness")
    for mutation in ("no_action", "other_exit", "missing_log", "missing_attempt", "nonterminal", "no_file"):
        changed, report = json.loads(json.dumps(row)), json.loads(json.dumps(verdict))
        if mutation == "no_action":
            changed["actions"]["tests"] = []
        elif mutation == "other_exit":
            changed["actions"]["tests"][0]["exit_code"] = 1
        elif mutation == "missing_log":
            changed["original_log_diagnostics"] = []
        elif mutation == "missing_attempt":
            report["executed"] = []
        elif mutation == "nonterminal":
            report["bep_finish"] = {}
        else:
            changed["original_files"]["status"] = "INCOMPLETE"
        if observation(changed, report)["status"] != "INCOMPLETE":
            raise ValueError("The launch-failure predicate accepts " + mutation)
    return {"status": "PASS", "controls": 7}


def qualify(arch, variant, source, tmp, out, private, bazel):
    key = variant + "-" + arch
    work = private / key
    work.mkdir()
    public = out / key
    public.mkdir()
    row = {"source": source, "variant": variant, "arch": arch, "targets": TARGETS, "required_cases": CASES, "libc": ABI, "phases": {}, "status": "INCOMPLETE"}
    write(public / "receipt.json", row)
    (work / "commit").write_bytes(git("cat-file", "commit", source))
    (work / "before").write_bytes(git("diff", "HEAD", "--"))
    (work / "expected.txt").write_text("\n".join(TARGETS) + "\n")
    write(work / "cases.json", CASES)
    platform = "//test/rbe/empty_worker:" + arch + "_musl"
    target_platform = "//tools/bazeldefs:linux_" + arch
    row["target_platform"] = target_platform
    row["execution_platform"] = platform
    common = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch],
              "--compilation_mode=fastbuild", "--host_platform=" + platform,
              "--extra_execution_platforms=" + platform,
              "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
              "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
              "--noincompatible_exclude_starlark_flags_from_exec_config",
              "--workspace_status_command=", "--host_features=fully_static_link",
              "--incompatible_use_native_posix_test_wrapper", "--nocache_test_results",
              "--nobuild_event_json_file_path_conversion", "--jobs=400",
              "--remote_download_outputs=toplevel",
              "--remote_download_regex=bazel-out/[^/]+/bin/(test/syscalls/(fork_test_native|linux/fork_test)|test/runner/setup_container/setup_container)$",
              "--remote_accept_cached", "--test_timeout=90", "--test_env=GTEST_FILTER=ForkTest.Simple:ForkTest.Pipe:ForkTest.SharedMapping",
              "--noremote_upload_local_results",
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
            preflight = report.preflight(raw, code, "test", work / "cases.json")
            write(public / "analysis.json", preflight)
            if preflight["status"] != "PASS":
                return row
            extra = preflight["output_options"]
            binaries = next(arg.split("=", 1)[1] for arg in common if arg.startswith("--remote_download_regex="))
            extra = ["--remote_download_regex=(" + binaries + ")|(" + arg.split("=", 1)[1] + ")"
                     if arg.startswith("--remote_download_regex=") else arg for arg in extra]
            continue
        (work / "arguments").write_text("\n".join(args) + "\n")
        (work / "native_exit").write_text(str(code))
        (work / "after").write_bytes(git("diff", "HEAD", "--"))
        (work / "head").write_bytes(git("rev-parse", "HEAD"))
        execroot = Path(subprocess.check_output([*bazel, "info", "execution_root"], stderr=subprocess.DEVNULL, text=True).strip())
        sanitized = work / "sanitized.bep"
        capture = report.capture(raw, sanitized, execroot, work / "reports", False, (work / "analysis.bep",), "test")
        (work / "capture_exit").write_text(str(int(bool(capture["errors"]))))
        context = {"expected_source": source, "commit_file": "commit", "source_before": "before",
                   "source_after": "after", "final_head": "head", "executed_arguments": "arguments",
                   "native_exit": "native_exit", "capture_exit": "capture_exit",
                   "execution_finished_at": finished, "command_kind": "test"}
        write(work / "context.json", context)
        verdict = report.verdict(sanitized, report.manifest_path(sanitized), work / "context.json",
                                 work / "expected.txt", (work / "analysis.bep",), case_manifest=work / "cases.json")
        write(public / "tests.json", verdict)
        for xml in (work / "reports").rglob("test.xml"):
            relative = xml.relative_to(work / "reports")
            destination = public / "xml" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(xml, destination)
        row["original_log_diagnostics"] = failure_lines(work / "reports")
        row["tests_status"] = verdict["status"]
        row["status"] = verdict["status"]
        # Preserve the test verdict before any independent worker-log decoding.
        write(public / "receipt.json", row)
        row["actions"] = native_actions(work / "actions.compact", arch, tmp, variant)
        # Runtime status is retained even if output inspection later fails.
        write(public / "receipt.json", row)
        row["original_files"] = product_files(bazel, common, work, execroot, arch, variant)
        row["verified_at"] = now()
        states = [verdict["status"], row["actions"]["status"], row["original_files"]["status"]]
        row["status"] = "FAIL" if "FAIL" in states else "PASS" if states == ["PASS"] * 3 else "INCOMPLETE"
        row["observation"] = observation(row, verdict)
        row["observation_status"] = row["observation"]["status"]
        write(public / "receipt.json", row)
    return row


def main():
    tmp = Path(os.environ["RUNNER_TEMP"])
    out = tmp / "native-package-evidence"
    out.mkdir(exist_ok=True)
    private = tmp / "native-package-private"
    private.mkdir(exist_ok=True)
    write(out / "observation-controls.json", observation_controls())
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
    for variant, source, arch in ((variant, source, arch) for variant, source in (("stock", STOCK), ("native_launcher", SOURCE)) for arch in ("amd64", "arm64")):
        key = variant + "-" + arch
        try:
            subprocess.run(["git", "checkout", "--detach", source], check=True, stdout=subprocess.DEVNULL)
            if git("rev-parse", "HEAD").decode().strip() != source or git("diff", "HEAD", "--").strip():
                raise ValueError("The control source was not selected cleanly")
            rows.append(qualify(arch, variant, source, tmp, out, private, bazel))
        except Exception as err:
            # Retain any already-written original result, including test failure.
            path = out / key / "receipt.json"
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
    comparison_status = "PASS" if len(rows) == 4 and all(row.get("observation_status") == "PASS" for row in rows) else "INCOMPLETE"
    summary = {"source": SOURCE, "stock_source": STOCK, "status": status, "observation_status": comparison_status, "rows": rows, "libc": ABI, "verified_at": now(),
               "native_bazel": {"supplier_source": "c2f5f79dce24877679cede14b85666b7ca65c6de",
                                "member": "bazel-candidate",
                                "sha256": binary_sha256,
                                "identity": "Canonical supplier build; static embedded ELF verified in supplier.json",
                                "wrapper_prerequisite": "https://github.com/bazelbuild/bazel/pull/30837",
                                "prior_native_xml_proof": "https://github.com/tamird/gvisor/actions/runs/41071795282/artifacts/12986015991"}}
    write(out / "summary.json", summary)
    return int(summary["observation_status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
