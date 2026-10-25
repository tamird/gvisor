"""Retain the canonical Ruby resolver output for the offline experiment."""

import hashlib
import json
from pathlib import Path
import shutil


def run(invoke, record_execution, evidence, private, output_root):
    target = "//test/rbe/ruby_official:resolve"
    result = invoke("ruby-gem-resolution", [
        "build", "--config=x86_64", "--compilation_mode=opt",
        "--lockfile_mode=error", "--noannounce_rc",
        "--remote_download_outputs=all",
        "--execution_log_compact_file=" + str(evidence / "native-execution.binpb"),
        target,
    ], "build", targets=[target], retain_outputs=True)
    if result["status"] != "PASS":
        raise RuntimeError("Official Ruby dependency resolution failed")
    archive = Path("bazel-bin/test/rbe/ruby_official/resolved-gems.tar")
    size = archive.stat().st_size
    if size > 16 * 1024 * 1024:
        raise ValueError(f"Resolved source archive exceeds the 16 MiB retention bound: {size}")
    destination = evidence / "resolved-gems.tar"
    shutil.copyfile(archive, destination)
    (evidence / "resolved-gems-record.json").write_text(json.dumps({
        "target": target,
        "output": str(archive),
        "bytes": size,
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
    }, indent=2) + "\n")
