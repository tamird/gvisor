"""Retain the public/full/minimal compiler contrast before interpreting it."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import qualification_report as report


OWNERS = ["//test/rbe/nogo_proto_compilers:validate_nogo",
          "//test/rbe/nogo_proto_compilers:docker_runtime_nogo"]
ARMS = ["public", "full", "minimal"]


def check_source(evidence: Path, phase: dict) -> Path:
    directory = evidence / ("invocation-" + phase["name"])
    context = json.loads((directory / "context.json").read_text())
    result = phase["result"]
    if (result["source"] != phase["source"]
            or context["expected_source"] != phase["source"]
            or (directory / "head.txt").read_text().strip() != phase["source"]
            or int((directory / "native-exit").read_text()) != result["native_exit"]
            or int((directory / "capture-exit").read_text()) != 0
            or (directory / "before.txt").read_text()
            or (directory / "after.txt").read_text()
            or (directory / "expected-targets.txt").read_text().splitlines() != OWNERS):
        raise ValueError(f"{phase['name']}: source/native/owner evidence differs")
    return directory


def check_green(evidence: Path, phase: dict, paths: set[Path]) -> dict:
    """Require two real uncached analyzer owners and exact native reports."""
    directory = check_source(evidence, phase)
    result = phase["result"]
    if result["status"] != "PASS" or result["native_exit"] != 0:
        raise ValueError(f"{phase['name']}: native result did not pass")
    attempts = result["executed"]
    if (len(attempts) != 2
            or {item["owner"]["label"] for item in attempts} != set(OWNERS)):
        raise ValueError(f"{phase['name']}: expected two actual owners")
    expected = set()
    for attempt in attempts:
        key = (attempt["owner"]["label"], attempt["owner"]["configuration"],
               attempt["run"], attempt["attempt"], attempt["shard"])
        if (key in expected or key[2:] != (1, 1, 1)
                or attempt["status"] != "PASSED" or attempt["cached_locally"]
                or attempt["execution_info"].get("cachedRemotely", False)
                or attempt["execution_info"].get("strategy") != "local"):
            raise ValueError(f"{phase['name']}: repeated, cached or failed attempt")
        expected.add(key)
    manifest_path = report.manifest_path(directory / "events.jsonl")
    manifest = json.loads(manifest_path.read_text())
    _, _, errors = report.retained_reports(manifest_path)
    if errors:
        raise ValueError(f"{phase['name']}: invalid retained reports: {errors}")
    bodies = {key: {} for key in expected}
    for row in manifest["reports"]:
        identity = row["identity"]
        key = (identity["label"], identity["configuration"]["id"],
               identity["run"], identity["attempt"], identity["shard"])
        path = (directory / row["path"]).resolve()
        path.relative_to(directory.resolve())
        if key not in expected or path in paths or row["name"] in bodies[key]:
            raise ValueError(f"{phase['name']}: unexpected report identity/path")
        if report.file_record(path) != {"bytes": row["bytes"], "sha256": row["sha256"]}:
            raise ValueError(f"{phase['name']}: retained report digest differs")
        paths.add(path)
        bodies[key][row["name"]] = path.read_bytes()
    census = []
    for key, reports in bodies.items():
        if set(reports) != {"test.log", "test.xml"}:
            raise ValueError(f"{phase['name']}: expected one log/XML per owner")
        cases = list(ET.fromstring(reports["test.xml"]).iter("testcase"))
        if len(cases) != 1 or list(cases[0]):
            raise ValueError(f"{phase['name']}: expected one positive XML per owner")
        log = reports["test.log"].decode()
        if not all(f"{cpu}...PASS" in log for cpu in ("amd64", "arm64")):
            raise ValueError(f"{phase['name']}: missing actual analyzer CPU markers")
        census.append({"owner": key[0], "configuration": key[1],
                       "case": cases[0].attrib["name"], "analyzer_cpus": ["amd64", "arm64"]})
    return {"owners": sorted(census, key=lambda row: row["owner"]),
            "reports": manifest["reports"]}


def check_baseline(evidence: Path, phase: dict) -> dict:
    directory = check_source(evidence, phase)
    result = phase["result"]
    if result["status"] != "FAIL" or result["native_exit"] == 0:
        raise ValueError("Public baseline did not retain a genuine native failure")
    text = (directory / "stderr.log").read_text()
    errors = []
    for generated in ("validate.pb.go", "plugin.pb.go"):
        matches = [line for line in text.splitlines() if "error checking types:" in line
                   and generated + ":" in line
                   and 'could not import google.golang.org/protobuf/reflect/protoreflect' in line
                   and 'unable to find "google.golang.org/protobuf/reflect/protoreflect" archive' in line]
        if not matches or not all(any(cpu in line for line in matches) for cpu in ("k8-", "aarch64-")):
            raise ValueError(f"Missing both-CPU baseline diagnostic for {generated}")
        errors.extend(matches)
    for owner in ("//validate:validate_go", "//api/types/swarm/runtime:runtime"):
        if not any(owner in line and "failed:" in line for line in text.splitlines()):
            raise ValueError(f"Missing baseline failed action owner {owner}")
    return {"diagnostics": errors, "stderr": report.file_record(directory / "stderr.log"),
            "native_status": result["status"], "native_exit": result["native_exit"]}


def check_action_semantics(evidence: Path, record: dict, config: dict) -> dict:
    """Bind successful typechecking to generated sources and runtime imports."""
    generated = {
        "protoc-gen-validate+//validate:validate_go": "validate.pb.go",
        "gazelle++go_deps+com_github_docker_docker//api/types/swarm/runtime:runtime": "plugin.pb.go",
    }
    result = {}
    for arm in record["arms"]:
        if arm["name"] == "public":
            continue
        path = evidence / ("x86_64-" + arm["name"] + "-image-execution.json")
        proof = json.loads(path.read_text())
        if proof["status"] != "PASS":
            raise ValueError(f"{arm['name']}: native action capture failed")
        selected = {}
        for label, filename in generated.items():
            rows = [row for row in proof["image_actions"]
                    if row["targetLabel"].lstrip("@") == label
                    and row["mnemonic"] == "GoStaticAnalysis"
                    and "check" in row["commandArgs"]]
            cpus = set()
            for row in rows:
                args = row["commandArgs"]
                arch = [arg.removeprefix("-GOARCH=") for arg in args if arg.startswith("-GOARCH=")]
                if len(arch) != 1 or arch[0] in cpus:
                    raise ValueError(f"{arm['name']}/{label}: duplicate or absent CPU")
                cpus.add(arch[0])
                if (row["runner"] != "remote" or row["cacheHit"] or row["exitCode"]
                        or row["status"] or not row["digest"]["hash"] or not row["actualOutputs"]):
                    raise ValueError(f"{arm['name']}/{label}: no actual successful remote analysis")
                if not any(arg.endswith("/" + filename) for arg in args):
                    raise ValueError(f"{arm['name']}/{label}: generated source absent")
                imports = {}
                for package in ("google.golang.org/protobuf/reflect/protoreflect",
                                "google.golang.org/protobuf/runtime/protoimpl"):
                    prefix = "-import=" + package + "="
                    matches = [arg.removeprefix(prefix) for arg in args if arg.startswith(prefix)]
                    if len(set(matches)) != 1:
                        raise ValueError(f"{arm['name']}/{label}: missing runtime import {package}")
                    imports[package] = matches[0]
                selected[label + "/" + arch[0]] = {"action": row, "runtime_imports": imports}
            if cpus != {"amd64", "arm64"} or len(rows) != 2:
                raise ValueError(f"{arm['name']}/{label}: missing both CPU analyses")
        result[arm["name"]] = {"actions": selected, "execution_log": proof["execution_log"],
                               "uncompressed": proof["uncompressed"]}
    if set(result) != {"full", "minimal"}:
        raise ValueError("Missing green-arm action proof")
    return result


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    arms = config["arms"]
    if (config.get("draft", True) or config["targets"] != OWNERS
            or [arm["name"] for arm in arms] != ARMS
            or len({arm["source"] for arm in arms}) != 3
            or report.source_set(config["expected_sources"]) != {arm["source"] for arm in arms}
            or config["source"] != arms[-1]["source"]):
        raise ValueError("Unbound or unexpected three-arm contrast contract")
    options = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"],
               "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
    selected = ["--strategy=TestRunner=local", "--nocache_test_results", "--runs_per_test=1",
                "--flaky_test_attempts=1", "--test_output=all", "--test_timeout=300",
                "--test_sharding_strategy=disabled", "--test_tag_filters=", "--keep_going",
                "--modify_execution_info=^GoStaticAnalysis$=+no-remote-cache"]
    record = {"contract_status": "INCOMPLETE", "source": config["source"],
              "expected_sources": config["expected_sources"], "arms": [], "restored": False,
              "scope": "Native baseline FAIL is preserved; green arms establish focused analyzer coverage only."}
    try:
        for arm in arms:
            source = arm["source"]
            subprocess.run(["git", "checkout", "--detach", source], check=True, timeout=30)
            if (subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != source
                    or subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"])):
                raise ValueError("Source switch did not reach a clean arm")
            for name, expected in arm["matched_inputs"].items():
                path = Path(name)
                body = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
                if hashlib.sha256(body).hexdigest() != expected:
                    raise ValueError(f"{arm['name']}: source input differs: {name}")
            name = arm["name"] + "-nogo"
            extra = ([] if arm["name"] == "public" else
                     ["--execution_log_json_file=" + str(private / (arm["name"] + "-execution.json"))])
            result = invoke(name, ["test", *options, *selected, *extra, *OWNERS],
                            "test", targets=OWNERS, source=source)
            record["arms"].append({"name": arm["name"], "source": source,
                                   "product": arm["product"], "phase": {
                                       "name": name, "source": source, "result": result}})
        # No baseline or report interpretation is allowed before all three
        # native test commands return. The shared driver preserves each exit.
        observed = {path.parent.name.removeprefix("invocation-"):
                    json.loads(path.read_text())["source"]
                    for path in evidence.glob("invocation-*/verdict.json")}
        if observed != config["expected_phase_sources"]:
            raise ValueError("Actual phase/source mapping differs from the plan")
        capture_errors = []
        for arm in record["arms"]:
            if arm["name"] == "public":
                continue
            try:
                record_execution(private / (arm["name"] + "-execution.json"), "x86_64", arm["name"])
            except (OSError, ValueError, RuntimeError) as error:
                capture_errors.append(f"{arm['name']}: {error}")
        if capture_errors:
            raise ValueError("; ".join(capture_errors))
        paths = set()
        for arm in record["arms"]:
            phase = arm["phase"]
            phase["checked"] = (check_baseline(evidence, phase) if arm["name"] == "public"
                                else check_green(evidence, phase, paths))
        record["semantic_proof"] = check_action_semantics(evidence, record, config)
        record["contract_status"] = "PASS"
        record["native_baseline_status"] = "FAIL"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        record.update(contract_status="FAIL", error=str(error))
        raise
    finally:
        try:
            subprocess.run(["git", "checkout", "--detach", config["source"]], check=True, timeout=30)
            if (subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != config["source"]
                    or subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"])):
                raise ValueError("Failed to restore initial qualification source")
            record["restored"] = True
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            record.update(contract_status="FAIL", restoration_error=str(error))
            raise
        finally:
            report.write_json(evidence / "nogo-proto-contrast.json", record)
