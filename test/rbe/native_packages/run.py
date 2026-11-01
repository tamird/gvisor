"""Run the original C++ PosixError suite on both native scratch platforms."""

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
IMAGE = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
TARGETS = ["//test/util:posix_error_test"]
LOADER_ARGS = ["test/rbe/native_glibc/loader", "--inhibit-cache", "--library-path", "test/rbe/native_glibc/lib"]
LOADER_DIGESTS = {
    "amd64": "41daf7836f7fce21a40ca63a00c5caf21b05a35a76ccc075d86f1716212cdf01",
    "arm64": "1f4fe51e11324b5bcc01ef1fd3859b5dd344ec16a8674d74ce618ef65016fbaf",
}
CASES = {TARGETS[0]: ["PosixErrorTest/PosixError", "PosixErrorTest/PosixErrorOrPosixError", "PosixErrorTest/PosixErrorOrNullptr"]}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args])


def original_tests(tests, targets, wrapper="compiled_posix_test_wrapper", *, run_under=False):
    if collections.Counter(t["target"] for t in tests) != collections.Counter(targets):
        return False
    for test in tests:
        package, name = test["target"].removeprefix("//").split(":")
        if run_under and test.get("arguments") != [*LOADER_ARGS, test["test_binary"]]:
            return False
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
    projection = 'select(has("spawn")) | .spawn | {m:.mnemonic,l:.targetLabel,x:.args[0],t:(if .mnemonic == "TestRunner" then .args[1:] else null end),r:.runner,c:.cacheHit,s:.status,e:.exitCode,d:.digest,p:(.platform.properties // [])}'
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
                    item["test_binary"] = row["t"][-1] if row["t"] else ""
                    tests.append(item)
                if (not correct or runner not in ("remote", "remote cache hit") or row["s"] or row["e"]) and len(bad) < 30:
                    bad.append(item)
            projection_exit = projected.wait()
        decode_exit = decoder.wait()
    valid_tests = original_tests(tests, TARGETS, "posix_tw" if arch == "amd64" else "compiled_posix_test_wrapper", run_under=True)
    return {"status": "PASS" if counts["total"] and not bad and not decode_exit and not projection_exit and valid_tests else "FAIL",
            "counts": dict(counts), "decoder_exits": [decode_exit, projection_exit],
            "tests": tests, "failed_or_other": bad}


def product_elf(bazel, common, work, execroot, arch):
    with (work / "output-query.log").open("w") as log:
        result = subprocess.run(["timeout", "--signal=INT", "--kill-after=25s", "8m",
                                 *bazel, "cquery", *common, "--output=files", "set(" + TARGETS[0] + " @qualification_glibc_" + arch + "//:libraries)"],
                                stdout=subprocess.PIPE, stderr=log, text=True)
    if result.returncode:
        return {"status": "INCOMPLETE", "native_exit": result.returncode}
    files = [line for line in result.stdout.splitlines() if Path(line).name == "posix_error_test"]
    if len(files) != 1 or not (execroot / files[0]).is_file():
        return {"status": "INCOMPLETE", "reason": "The original test executable was not materialized locally"}
    binary = execroot / files[0]
    data = elf(binary.read_bytes())
    data["target"] = TARGETS[0]
    # Inspect the declared source artifacts, independent of local runfiles materialization.
    libraries = [execroot / path for path in result.stdout.splitlines() if path not in files]
    loader_name = {"amd64": "ld-linux-x86-64.so.2", "arm64": "ld-linux-aarch64.so.1"}[arch]
    loaders = [path for path in libraries if path.name == loader_name]
    if len(loaders) != 1:
        return {"status": "INCOMPLETE", "reason": "The declared GNU loader was not found"}
    loader = elf(loaders[0].read_bytes())
    data["declared_loader"] = loader
    data["runtime_files"] = [{"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                             for path in sorted(libraries)]
    data["status"] = "PASS" if (data["machine"] == {"amd64": 62, "arm64": 183}[arch]
                                and len(data["interpreter"]) == 1
                                and Path(data["interpreter"][0]).name == loader_name
                                and "libc.so.6" in data["needed"]
                                and loader["sha256"] == LOADER_DIGESTS[arch]
                                and not loader["interpreter"] and not loader["needed"]) else "FAIL"
    return data


def qualify(arch, tmp, out, private, bazel):
    work = private / arch
    work.mkdir()
    public = out / arch
    public.mkdir()
    row = {"source": SOURCE, "arch": arch, "targets": TARGETS, "required_cases": CASES, "phases": {}, "status": "INCOMPLETE"}
    write(public / "receipt.json", row)
    (work / "commit").write_bytes(git("cat-file", "commit", SOURCE))
    (work / "before").write_bytes(git("diff", "HEAD", "--"))
    (work / "expected.txt").write_text("\n".join(TARGETS) + "\n")
    write(work / "cases.json", CASES)
    platform = "//test/rbe/empty_worker:" + arch + "_musl"
    common = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch],
              "--compilation_mode=fastbuild", "--host_platform=" + platform,
              "--extra_execution_platforms=" + platform,
              "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
              "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
              "--noincompatible_exclude_starlark_flags_from_exec_config",
              "--workspace_status_command=", "--host_features=fully_static_link",
              "--run_under=//test/rbe/native_glibc:loader " + " ".join(LOADER_ARGS[1:]),
              "--incompatible_use_native_posix_test_wrapper", "--nocache_test_results",
              "--nobuild_event_json_file_path_conversion", "--jobs=400",
              "--remote_download_outputs=toplevel",
              "--remote_download_regex=bazel-out/[^/]+/bin/test/util/posix_error_test$",
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
                                 work / "expected.txt", (work / "analysis.bep",), case_manifest=work / "cases.json")
        write(public / "tests.json", verdict)
        for xml in (work / "reports").rglob("test.xml"):
            relative = xml.relative_to(work / "reports")
            destination = public / "xml" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(xml, destination)
        row["tests_status"] = verdict["status"]
        row["status"] = verdict["status"]
        # Preserve the test verdict before any independent worker-log decoding.
        write(public / "receipt.json", row)
        row["actions"] = native_actions(work / "actions.compact", arch, tmp)
        # Runtime status is retained even if output inspection later fails.
        write(public / "receipt.json", row)
        row["product_elf"] = product_elf(bazel, common, work, execroot, arch)
        row["verified_at"] = now()
        states = [verdict["status"], row["actions"]["status"], row["product_elf"]["status"]]
        row["status"] = "FAIL" if "FAIL" in states else "PASS" if states == ["PASS"] * 3 else "INCOMPLETE"
        write(public / "receipt.json", row)
    return row


def main():
    tmp = Path(os.environ["RUNNER_TEMP"])
    out = tmp / "native-package-evidence"
    out.mkdir(exist_ok=True)
    private = tmp / "native-package-private"
    private.mkdir(exist_ok=True)
    control = json.loads((tmp / "native-wrapper-control.json").read_text())
    actual = control["test_action"]
    targets = [actual["target"]]
    if not original_tests([actual], targets):
        raise ValueError("The verifier rejects the retained successful native TestRunner")
    if original_tests([dict(actual, executable="external/bazel_tools/tools/test/test-setup.sh")], targets):
        raise ValueError("The verifier accepts a shell TestRunner as native")
    wrapped = dict(actual, arguments=[*LOADER_ARGS, actual["test_binary"]])
    if not original_tests([wrapped], targets, run_under=True):
        raise ValueError("The verifier rejects the declared-loader command shape")
    for index in range(len(LOADER_ARGS) + 1):
        wrong = dict(wrapped, arguments=[*wrapped["arguments"]])
        wrong["arguments"][index] = "incorrect"
        if original_tests([wrong], targets, run_under=True):
            raise ValueError("The verifier accepts an incorrect loader or original binary argument")
    write(out / "wrapper-control.json", {"status": "PASS", "provenance": control["provenance"],
                                        "loader_shape_positive": 1, "loader_shape_negative": 5})
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
    for arch in ("amd64", "arm64"):
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
