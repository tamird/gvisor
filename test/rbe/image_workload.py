"""Compare native gem payloads built under two distinct Bazel configurations."""

import hashlib
import json
from pathlib import Path
import shutil


def run(invoke, record_execution, evidence, private, output_root):
    targets = ["//test/rbe/ruby_official:gems_" + arch for arch in ("amd64", "arm64")]
    records = []
    profiles = {}
    payloads = {}
    paths = []
    for mode in ("fastbuild", "opt"):
        phase = "ruby-native-" + mode
        result = invoke(phase, [
            "build", "--config=rbe-matrix", "--config=x86_64", "--compilation_mode=" + mode,
            "--lockfile_mode=error", "--noannounce_rc", "--keep_going",
            "--remote_download_outputs=all",
            "--execution_log_compact_file=" + str(evidence / (phase + "-execution.binpb")),
            *targets,
        ], "build", targets=targets, retain_outputs=True)
        if result["status"] != "PASS":
            raise RuntimeError("Official Ruby native " + mode + " build failed")
        for arch in ("amd64", "arm64"):
            for suffix in ("json", "tar"):
                archive = Path("bazel-bin/test/rbe/ruby_official") / f"gems_{arch}.{suffix}"
                size = archive.stat().st_size
                if size > 32 * 1024 * 1024:
                    raise ValueError(f"Native gem output exceeds the 32 MiB retention bound: {archive}: {size}")
                destination = evidence / f"{mode}-gems_{arch}.{suffix}"
                shutil.copyfile(archive, destination)
                digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                records.append({"architecture": arch, "mode": mode, "output": str(archive), "bytes": size, "sha256": digest})
                if suffix == "json":
                    profiles[arch, mode] = (size, digest)
                else:
                    payloads[arch, mode] = (size, digest)
        directory = evidence / ("invocation-" + phase)
        paths.extend(json.loads(line.removeprefix("NATIVE_GEM_BUILD_DIRECTORY "))
                     for name in ("stdout.log", "stderr.log")
                     for line in (directory / name).read_text().splitlines()
                     if line.startswith("NATIVE_GEM_BUILD_DIRECTORY "))
    (evidence / "native-gems-record.json").write_text(json.dumps(records, indent=2) + "\n")
    for arch in ("amd64", "arm64"):
        if profiles[arch, "fastbuild"] != profiles[arch, "opt"]:
            raise ValueError(f"{arch} native gem profile differs across configurations")
        if payloads[arch, "fastbuild"] != payloads[arch, "opt"]:
            raise ValueError(f"{arch} native gem tar bytes differ across configurations")
    if len(paths) != 4 or len({row["directory"] for row in paths}) != 4 or len({row["output"] for row in paths}) != 4:
        raise ValueError("Expected four actual distinct native build paths in action output")
    (evidence / "native-repeatability.json").write_text(json.dumps({"status": "PASS", "payloads": {arch: payloads[arch, "fastbuild"] for arch in ("amd64", "arm64")}}, indent=2) + "\n")
