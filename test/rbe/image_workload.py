"""Probe the unchanged GNU baseline on its pinned upstream execution image."""

import json
import os
from pathlib import Path

import qualification_report as report


def probe(invoke, record_execution, evidence: Path, private: Path, config: dict, architecture: str, record: dict) -> None:
    targets = config["targets"]
    options = ["--config=" + architecture, "--compilation_mode=opt", "--lockfile_mode=error",
               "--noannounce_rc", "--remote_download_outputs=all",
               "--build_event_json_file_path_conversion=false"]
    phase = architecture + "-gnu-probe"
    try:
        result = invoke(architecture + "-execution-root", ["info", *options, "execution_root"], "command", targets=[])
        if result["status"] != "PASS":
            raise RuntimeError("Cannot identify the canonical execution root")
        root = Path((evidence / ("invocation-" + architecture + "-execution-root/stdout.log")).read_text().strip())
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("Invalid canonical execution root")
        execution = private / (phase + "-execution.json")
        built = invoke(phase, ["build", *options, "--noremote_accept_cached",
                       "--keep_going", "--execution_log_json_file=" + str(execution),
                       "--noexecution_log_sort", *targets], "build", targets=targets)
        record["native_build_status"] = built["status"]
        if execution.is_file():
            record_execution(execution, architecture, "gnu-probe")
        else:
            raise RuntimeError("Native GNU execution log missing")
        if built["status"] != "PASS":
            raise RuntimeError("GNU worker probe failed")
        proof = json.loads((evidence / (phase + "-image-execution.json")).read_text())
        actions = proof["image_actions"]
        if proof["status"] != "PASS" or {action["targetLabel"] for action in actions} != set(targets):
            raise ValueError("Expected exact GNU probe action owners")
        if len(actions) != len(targets) or any(action["runner"] != "remote" or action["cacheHit"] for action in actions):
            raise ValueError("Every selected GNU action must actually execute remotely")
        owners = {(report.Owner(owner["label"], owner["configuration"]), "default") for owner in built["expected"]}
        outputs = report.advertised_build_outputs(evidence / ("invocation-" + phase + "/events.jsonl"), owners)
        if len(outputs) != len(targets) or {output["identity"]["label"] for output in outputs} != set(targets):
            raise ValueError("Expected one configured output per GNU owner")
        for output in outputs:
            name = Path(output["name"])
            if name.is_absolute() or ".." in name.parts or name.parts[:1] != ("bazel-out",):
                raise ValueError("Unexpected configured GNU output path")
            path = root / name
            actual = report.file_record(path)
            if any(actual[key] != output[key] for key in ("bytes", "sha256")):
                raise ValueError("GNU output differs from default BEP identity")
            matches = [item for action in actions for item in action["actualOutputs"] if item["path"] == output["name"]]
            if len(matches) != 1 or matches[0]["digest"]["hash"] != output["sha256"] or int(matches[0]["digest"]["sizeBytes"]) != output["bytes"]:
                raise ValueError("GNU output differs from actual remote action")
            data = path.read_bytes()
            label = output["identity"]["label"]
            if label == "//test/rbe/gnu_worker:compiler_identity":
                text = data.decode()
                if not text.startswith(f"compiler=/usr/bin/{architecture}-linux-gnu-gcc-11\n") or "gcc-11\t" not in text:
                    raise ValueError("Unexpected GNU compiler identity")
                (evidence / (architecture + "-gnu-compiler-identity.txt")).write_bytes(data)
            else:
                expected_type = 3 if label == "//vdso:vdso" else 1
                machine = 247 if label.startswith("//runsc/sandbox/bpf:") else {"x86_64": 62, "aarch64": 183}[architecture]
                if data[:6] != b"\x7fELF\x02\x01" or int.from_bytes(data[16:18], "little") != expected_type or int.from_bytes(data[18:20], "little") != machine:
                    raise ValueError(f"Unexpected ELF type/machine for {label}")
            output["materialized"] = actual
        record.update(status="PASS", outputs=outputs, uncached_remote_actions=len(actions))
    finally:
        report.write_json(evidence / (phase + ".json"), record)


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    record = {"status": "INCOMPLETE", "source": config["source"],
              "baseline_source": config["baseline_source"], "worker_image": config["worker_image"],
              "scope": "Uncached remote GNU, cross-GNU and BPF actions; no full release or startup timing.",
              "architectures": []}
    try:
        for architecture in ("x86_64", "aarch64"):
            current = {"architecture": architecture, "status": "INCOMPLETE"}
            record["architectures"].append(current)
            probe(invoke, record_execution, evidence, private, config, architecture, current)
        record["status"] = "PASS"
    finally:
        report.write_json(evidence / "gnu-worker-probe.json", record)
