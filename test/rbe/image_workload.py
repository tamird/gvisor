"""Retain the two native offline gem builds and their ABI profiles."""

import hashlib
import json
from pathlib import Path
import shutil


def run(invoke, record_execution, evidence, private, output_root):
    targets = ["//test/rbe/ruby_official:gems_" + arch for arch in ("amd64", "arm64")]
    result = invoke("ruby-native-gems", [
        "build", "--config=rbe-matrix", "--config=x86_64", "--compilation_mode=opt",
        "--lockfile_mode=error", "--noannounce_rc",
        "--keep_going",
        "--remote_download_outputs=all",
        "--execution_log_compact_file=" + str(evidence / "native-execution.binpb"),
        *targets,
    ], "build", targets=targets, retain_outputs=True)
    if result["status"] != "PASS":
        raise RuntimeError("Official Ruby native gem build failed")
    records = []
    for arch in ("amd64", "arm64"):
        for suffix in ("json", "tar"):
            archive = Path(f"bazel-bin/test/rbe/ruby_official/gems_{arch}.{suffix}")
            size = archive.stat().st_size
            if size > 32 * 1024 * 1024:
                raise ValueError(f"Native gem output exceeds the 32 MiB retention bound: {archive}: {size}")
            destination = evidence / archive.name
            shutil.copyfile(archive, destination)
            records.append({
                "target": "//test/rbe/ruby_official:gems_" + arch,
                "output": str(archive),
                "bytes": size,
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            })
    (evidence / "native-gems-record.json").write_text(json.dumps(records, indent=2) + "\n")
