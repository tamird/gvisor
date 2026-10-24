"""Run the canonical formatter with this source's declared Go SDK."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import qualification_report as report


SDK = "@io_bazel_rules_go//go:go"
PYTHON = "@rules_python//python/bin:python"


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    record = {"source": config["source"], "product": config["product"],
              "status": "INCOMPLETE", "checks": []}
    try:
        if config["targets"] != [] or config["image_targets"] != {"x86_64": [SDK, PYTHON]}:
            raise ValueError("Formatting recovery must not select behavioral tests")
        for path, expected in config["matched_inputs"].items():
            actual = subprocess.check_output(["git", "show", config["source"] + ":" + path])
            if hashlib.sha256(actual).hexdigest() != expected:
                raise ValueError(f"Frozen source input differs: {path}")
        options = ["--config=x86_64", "--compilation_mode=opt", "--lockfile_mode=error",
                   "--remote_download_outputs=all", "--noannounce_rc", *config["consumer_options"]]
        sdk = invoke("declared-go-sdk", ["run", *options, SDK, "--", "env", "-json", "GOROOT", "GOVERSION"],
                     "command", targets=[])
        record["checks"].append({"name": "declared-go-sdk", "result": sdk})
        if sdk["status"] != "PASS":
            raise RuntimeError("Declared Go SDK query failed")
        env_path = evidence / "invocation-declared-go-sdk" / "stdout.log"
        match json.loads(env_path.read_text()):
            case {"GOROOT": str(root), "GOVERSION": "go1.27.3"}:
                sdk_root = Path(root)
            case other:
                raise ValueError(f"Unexpected declared Go SDK: {other}")
        if not sdk_root.is_absolute() or not sdk_root.is_dir():
            raise ValueError(f"SDK root is not an existing absolute directory: {sdk_root}")
        record["sdk_environment"] = report.file_record(env_path)
        record["tools"] = {}
        for name in ("go", "gofmt"):
            path = sdk_root / "bin" / name
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ValueError(f"SDK tool is not executable: {path}")
            record["tools"][name] = report.file_record(path)
        original_path = os.environ["PATH"]
        try:
            os.environ["PATH"] = str(sdk_root / "bin") + os.pathsep + original_path
            selected_go = shutil.which("go")
            if selected_go is None or not (sdk_root / "bin" / "go").samefile(selected_go):
                raise ValueError("Canonical lint would not select the declared Go SDK")
            formatted = invoke("gofmt", ["run", *options, PYTHON, "--", "-c",
                               'import os, subprocess, sys; os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"]); sys.exit(subprocess.run(["bash", "tools/lint.sh", "gofmt"]).returncode)'],
                               "command", targets=[])
            record["checks"].append({"name": "gofmt", "result": formatted})
        finally:
            os.environ["PATH"] = original_path
        if formatted["status"] != "PASS":
            raise RuntimeError("Canonical gofmt failed")
        record["status"] = "PASS"
    except Exception as error:
        record["status"] = "FAIL"
        record["error"] = str(error)
        raise
    finally:
        report.write_json(evidence / "lock-format.json", record)
