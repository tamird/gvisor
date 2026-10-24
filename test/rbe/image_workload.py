"""Inspect real AtomicPtr code generation with the declared Bazel compiler."""
import hashlib
import json
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import qualification_report as report


ATOMIC = "//pkg/sync/atomicptr:atomicptr"
TEST = "//pkg/sync/atomicptr:atomicptr_test"
KVM = "//pkg/sentry/platform/kvm:kvm"
ANALYZERS = ["//pkg/sync/atomicptr:atomicptr_nogo", "//pkg/sync/atomicptr:atomicptr_test_nogo"]


def assembly(text: str, symbol: str) -> dict:
    starts = list(re.finditer(r"^(\S+) STEXT[^\n]*$", text, re.MULTILINE))
    blocks = [text[item.start():starts[index + 1].start() if index + 1 < len(starts) else len(text)]
              for index, item in enumerate(starts) if item.group(1) == symbol]
    if not blocks:
        raise ValueError("Missing materialized compiler function: " + symbol)
    results = []
    for block in blocks:
        instructions = []
        for line in block.splitlines():
            match = re.match(r"\s*0x[0-9a-f]+\s+\d+\s+\([^\n]*?\)\s+(.+)$", line)
            if match and not match.group(1).startswith(("TEXT", "FUNCDATA", "PCDATA")):
                instructions.append(match.group(1))
        if not instructions:
            raise ValueError("Missing actual compiler instructions: " + symbol)
        calls = [line for line in instructions if re.search(r"\b(?:CALL|BL|BLR)\b", line)]
        header = block.splitlines()[0]
        locals_match = re.search(r"\blocals=(0x[0-9a-f]+|\d+)", header)
        if locals_match is None:
            raise ValueError("Missing compiler frame size: " + symbol)
        text_lines = [line for line in block.splitlines() if "\tTEXT\t" in line]
        if len(text_lines) != 1:
            raise ValueError("Missing unique TEXT directive: " + symbol)
        flags = text_lines[0].split(",", 1)[1].rsplit(",", 1)[0].strip().split("|")
        abi = "ABIWRAPPER" if "ABIWRAPPER" in flags else "ABIInternal" if "ABIInternal" in flags else None
        if abi is None:
            raise ValueError("Unrecognized compiler ABI: " + text_lines[0])
        results.append({"symbol": symbol, "abi": abi, "header": header, "locals_bytes": int(locals_match.group(1), 0),
                        "instructions": instructions, "calls": calls})
    internal = [row for row in results if row["abi"] == "ABIInternal"]
    if len(internal) != 1:
        raise ValueError("Expected one ABIInternal compiler body: " + symbol)
    return {**internal[0], "abi_wrappers": [row for row in results if row["abi"] == "ABIWRAPPER"]}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    for name, expected in config["matched_inputs"].items():
        path = Path(name)
        body = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        if hashlib.sha256(body).hexdigest() != expected:
            raise ValueError("Source input differs: " + name)
    common = ["--compilation_mode=opt", *config["consumer_options"], "--lockfile_mode=error",
              "--remote_download_outputs=all", "--noannounce_rc"]
    result = {"status": "INCOMPLETE", "source": config["source"], "architectures": {}, "errors": []}
    try:
        formatted = invoke("goimports", ["run", "--config=x86_64", *common, "--run_in_cwd",
                            "@org_golang_x_tools//cmd/goimports", "--", "-format-only", "-d",
                            "pkg/sync/atomicptr/inline_probe.go", "pkg/sync/atomicptr/inline_probe_test.go"],
                           "command", targets=[])
        if formatted["status"] != "PASS" or (evidence / "invocation-goimports/stdout.log").read_bytes():
            raise RuntimeError("Canonical Go formatting failed")
        compiler_texts = {}
        for architecture in ("x86_64", "aarch64"):
            entry = {"phases": {}, "probes": {}, "wrapper_inline_diagnostics": []}
            result["architectures"][architecture] = entry
            for package, target in (("atomicptr", ATOMIC), ("kvm", KVM)):
                name = package + "-codegen-" + architecture
                verdict = invoke(name, ["build", "--config=" + architecture, *common,
                                       "--noremote_accept_cached", "--experimental_ui_max_stdouterr_bytes=16777216", target], "build", targets=[target])
                entry["phases"][package] = verdict
                if verdict["status"] != "PASS":
                    result["errors"].append("Native code-generation build failed: " + name)
                directory = evidence / ("invocation-" + name)
                text = "\n".join((directory / stream).read_text() for stream in ("stdout.log", "stderr.log"))
                entry[package + "_compiler_output"] = [report.file_record(directory / stream) for stream in ("stdout.log", "stderr.log")]
                compiler_texts[(architecture, package)] = text
        linked = invoke("atomicptr-link-aarch64", ["build", "--config=aarch64", *common, TEST], "build", targets=[TEST])
        selected = ["--strategy=TestRunner=local", "--nocache_test_results", "--runs_per_test=1",
                    "--flaky_test_attempts=1", "--test_sharding_strategy=disabled", "--test_output=all",
                    "--test_timeout=180", "--test_tag_filters=", "--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=1"]
        normal = invoke("atomicptr-normal", ["test", "--config=x86_64", *common, *selected, TEST], "test", targets=[TEST])
        nogo = invoke("atomicptr-nogo", ["test", "--config=x86_64", *common, *selected, *ANALYZERS], "test", targets=ANALYZERS)
        result.update(arm64_link=linked, normal=normal, nogo=nogo)
        # Finish every native phase before interpreting compiler diagnostics.
        for architecture in ("x86_64", "aarch64"):
            entry = result["architectures"][architecture]
            try:
                for package in ("atomicptr", "kvm"):
                    text = compiler_texts[(architecture, package)]
                    entry["wrapper_inline_diagnostics"].extend(line for line in text.splitlines()
                        if ("can inline" in line or "cannot inline" in line or "inlining call to" in line)
                        and ("AtomicPtr" in line or "sync/atomic.(*Pointer" in line))
                    if package == "atomicptr":
                        for function in ("loadBefore", "loadAfter", "storeBefore", "storeAfter", "swapBefore", "swapAfter", "loadNoInline", "loadControl"):
                            entry["probes"][function] = assembly(text, "gvisor.dev/gvisor/pkg/sync/atomicptr." + function)
                    else:
                        entry["real_nosplit_caller"] = assembly(text, "gvisor.dev/gvisor/pkg/sentry/platform/kvm.seccompMmapHandler")
                probes = entry["probes"]
                entry["negative_control_detected"] = any("loadNoInline" in line for line in probes["loadControl"]["calls"])
                if not entry["negative_control_detected"]:
                    result["errors"].append(architecture + ": deliberate direct call was not detected")
                for operation in ("load", "store", "swap"):
                    before, after = probes[operation + "Before"], probes[operation + "After"]
                    after["same_instructions_as_before"] = after["instructions"] == before["instructions"]
                    after["frame_delta_bytes"] = after["locals_bytes"] - before["locals_bytes"]
                    if any("AtomicPtr" in line or "sync/atomic.(*Pointer" in line for line in after["calls"]):
                        result["errors"].append(architecture + ": residual typed wrapper call in " + operation)
                for key in ("loadBefore", "loadAfter"):
                    if probes[key]["calls"] or "nosplit" not in probes[key]["header"].lower():
                        result["errors"].append(architecture + ": load probe has a call or lacks NOSPLIT")
                real = entry["real_nosplit_caller"]
                if "nosplit" not in real["header"].lower() or any("machineAtomicPtr" in line or "sync/atomic.(*Pointer" in line for line in real["calls"]):
                    result["errors"].append(architecture + ": real signal handler has a wrapper call or lacks NOSPLIT")
            except (ValueError, KeyError) as error:
                result["errors"].append(architecture + ": " + str(error))
        if any(row["status"] != "PASS" for row in (linked, normal, nogo)):
            raise RuntimeError("Native link/correctness/analyzer gate failed")
        for name, owners in (("atomicptr-normal", [TEST]), ("atomicptr-nogo", ANALYZERS)):
            verdict = json.loads((evidence / ("invocation-" + name) / "verdict.json").read_text())
            executed = verdict["executed"]
            if len(executed) != len(owners) or {row["owner"]["label"] for row in executed} != set(owners):
                raise ValueError("Unexpected native attempt census: " + name)
            for row in executed:
                if (row["status"] != "PASSED" or row["cached_locally"]
                        or (row["run"], row["attempt"], row["shard"]) != (1, 1, 1)):
                    raise ValueError("Unexpected native attempt: " + str(row))
            _, reports, errors = report.retained_reports(report.manifest_path(evidence / ("invocation-" + name) / "events.jsonl"))
            if errors:
                raise ValueError(str(errors))
            for owner in owners:
                selected_reports = [(key, path) for key, path in reports.items() if key[0].owner.label == owner]
                if sorted(key[1] for key, _ in selected_reports) != ["test.log", "test.xml"]:
                    raise ValueError("Missing unique native reports: " + owner)
                log = next(path for key, path in selected_reports if key[1] == "test.log")
                xml = next(path for key, path in selected_reports if key[1] == "test.xml")
                cases = list(ET.parse(xml).getroot().iter("testcase"))
                if not cases or any(list(case) for case in cases):
                    raise ValueError("Failed, skipped or absent XML cases: " + owner)
                if owner == TEST:
                    if len(cases) != 2 or {case.attrib["name"] for case in cases} != {"TestAtomicPtr", "TestAtomicPtrInlineProbes"}:
                        raise ValueError("Unexpected correctness census")
                elif len(cases) != 1 or not all(cpu + "...PASS" in log.read_text() for cpu in ("amd64", "arm64")):
                    raise ValueError("Missing both-CPU analyzer results")
        if result["errors"]:
            raise RuntimeError("; ".join(result["errors"]))
        result["status"] = "PASS"
    except (OSError, ValueError, RuntimeError, ET.ParseError) as error:
        result["errors"].append(str(error))
        result["status"] = "FAIL"
        raise
    finally:
        report.write_json(evidence / "atomicptr-codegen.json", result)
