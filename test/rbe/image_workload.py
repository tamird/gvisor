"""Probe the unchanged GNU baseline on its pinned upstream execution image."""

import json
import os
from pathlib import Path

import qualification_report as report


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    targets = config["targets"]
    options = ["--config=x86_64", "--compilation_mode=opt", "--lockfile_mode=error",
               "--noannounce_rc", "--remote_download_outputs=all",
               "--build_event_json_file_path_conversion=false"]
    record = {"status": "INCOMPLETE", "source": config["source"],
              "baseline_source": config["baseline_source"], "worker_image": config["worker_image"],
              "scope": "Three uncached remote GNU actions only; no full release or startup timing."}
    try:
        result = invoke("execution-root", ["info", *options, "execution_root"], "command", targets=[])
        if result["status"] != "PASS":
            raise RuntimeError("Cannot identify the canonical execution root")
        root = Path((evidence / "invocation-execution-root/stdout.log").read_text().strip())
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("Invalid canonical execution root")
        execution = private / "gnu-probe-execution.json"
        built = invoke("gnu-probe", ["build", *options, "--noremote_accept_cached",
                       "--keep_going", "--execution_log_json_file=" + str(execution),
                       "--noexecution_log_sort", *targets], "build", targets=targets)
        record["native_build_status"] = built["status"]
        if execution.is_file():
            record_execution(execution, "x86_64", "gnu-probe")
        else:
            raise RuntimeError("Native GNU execution log missing")
        if built["status"] != "PASS":
            raise RuntimeError("GNU worker probe failed")
        proof = json.loads((evidence / "x86_64-gnu-probe-image-execution.json").read_text())
        actions = proof["image_actions"]
        if proof["status"] != "PASS" or {action["targetLabel"] for action in actions} != set(targets):
            raise ValueError("Expected exact GNU probe action owners")
        if len(actions) != len(targets) or any(action["runner"] != "remote" or action["cacheHit"] for action in actions):
            raise ValueError("Every selected GNU action must actually execute remotely")
        owners = {(report.Owner(owner["label"], owner["configuration"]), "default") for owner in built["expected"]}
        outputs = report.advertised_build_outputs(evidence / "invocation-gnu-probe/events.jsonl", owners)
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
            if output["identity"]["label"] == "//test/rbe/gnu_worker:compiler_identity":
                text = data.decode()
                if not text.startswith("compiler=/usr/bin/x86_64-linux-gnu-gcc-11\n") or "gcc-11\t" not in text:
                    raise ValueError("Unexpected GNU compiler identity")
                (evidence / "gnu-compiler-identity.txt").write_bytes(data)
            else:
                if data[:6] != b"\x7fELF\x02\x01" or int.from_bytes(data[16:18], "little") != 1 or int.from_bytes(data[18:20], "little") != 62:
                    raise ValueError("Expected AMD64 ELF relocatable compiler output")
            output["materialized"] = actual
        record.update(status="PASS", outputs=outputs, uncached_remote_actions=len(actions))
    finally:
        report.write_json(evidence / "gnu-worker-probe.json", record)
