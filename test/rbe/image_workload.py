"""Compile the pidfd collection and its two-architecture runtime images."""
import hashlib
import json
import os
from pathlib import Path


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    targets = config["targets"]
    image_targets = config["image_targets"]
    profile_targets = list(dict.fromkeys(row["owner"] for row in config["profile_helper_outputs"]))
    if set(image_targets).intersection(profile_targets) or set(targets) != set(image_targets + profile_targets):
        raise ValueError("PIDFD and profile-helper target partitions must cover the selected build")
    options = ["--compilation_mode=opt", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc",
               "--build_event_json_file_path_conversion=false"]
    root_name = "canonical-execution-root"
    result = invoke(root_name, ["info", *options, "execution_root"], "command", targets=[])
    if result["status"] != "PASS":
        raise RuntimeError("Cannot identify canonical execution root")
    execution_root = Path((evidence / ("invocation-" + root_name) / "stdout.log").read_text().strip())
    if not execution_root.is_absolute() or not execution_root.is_dir():
        raise ValueError(f"Invalid canonical execution root: {execution_root}")
    for architecture in ("x86_64", "aarch64"):
        platform = "--platforms=" + config["image_platforms"][architecture]
        for phase, selected, platform_options in (("pidfd", image_targets, [platform]),
                                                  ("profile-helper", profile_targets, [])):
            execution_log = evidence / (architecture + "-" + phase + "-execution.json")
            built = invoke(phase + "-" + architecture,
                           ["build", *options, "--config=" + architecture, *platform_options, "--keep_going",
                            "--execution_log_json_file=" + str(execution_log),
                            "--noexecution_log_sort", *selected], "build", targets=selected)
            if built["status"] != "PASS":
                raise RuntimeError(phase + " image build failed for " + architecture)
            record_execution(execution_log, architecture, phase)
        expected = [row for row in config["profile_helper_outputs"]
                    if row["architecture"] == architecture]
        comparisons = []
        destination = evidence / (architecture + "-profile-helper-equality.json")
        try:
            for index, row in enumerate(expected):
                name = f"profile-output-{architecture}-{index}"
                result = invoke(name, ["cquery", *options, "--config=" + architecture,
                                      "--output=files", row["owner"]],
                                "command", targets=[])
                if result["status"] != "PASS":
                    raise RuntimeError("Cannot locate profile-helper output")
                lines = (evidence / ("invocation-" + name) / "stdout.log").read_text().splitlines()
                match lines:
                    case [output]:
                        path = Path(output)
                        if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("bazel-out",):
                            raise ValueError(f"Unexpected image output path: {path}")
                        payload = (execution_root / path).read_bytes()
                    case _:
                        raise ValueError(f"Expected one output for {row['owner']}: {lines!r}")
                actual = {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
                wanted = {key: row[key] for key in ("sha256", "bytes")}
                comparisons.append({"owner": row["owner"], "actual": actual,
                                    "expected": wanted, "equal": actual == wanted})
                if actual != wanted:
                    raise ValueError(f"Profile-helper output changed: {row['owner']}")
        finally:
            destination.write_text(json.dumps(comparisons, indent=2) + "\n")
    if config.get("runtime", False):
        import image_pidfd_runtime
        image_pidfd_runtime.run(invoke, evidence, private)
