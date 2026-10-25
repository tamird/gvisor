"""Compare native gem payloads built under two distinct Bazel configurations."""

import hashlib
import json
from pathlib import Path
import shutil


def run(invoke, record_execution, evidence, private, output_root):
    targets = ["//test/rbe/ruby_official:gems_" + arch for arch in ("amd64", "arm64")]
    targets += ["//test/rbe/ruby_official:qualification_gems", "//test/rbe/ruby_official:qualification_profiles"]
    result = invoke("ruby-native-repeatability", [
        "build", "--config=rbe-matrix", "--config=x86_64", "--compilation_mode=opt",
        "--lockfile_mode=error", "--noannounce_rc", "--keep_going",
        "--remote_download_outputs=all",
        "--execution_log_compact_file=" + str(evidence / "native-execution.binpb"),
        *targets,
    ], "build", targets=targets, retain_outputs=True)
    if result["status"] != "PASS":
        raise RuntimeError("Official Ruby native repeatability build failed")
    records = []
    profiles = {}
    payloads = {}
    for arch in ("amd64", "arm64"):
        for configured in (False, True):
            for suffix in ("json", "tar"):
                name = f"gems_{arch}.{suffix}"
                if configured:
                    name = f"qualification_{'profile' if suffix == 'json' else 'gems'}_{arch}.{suffix}"
                archive = Path("bazel-bin/test/rbe/ruby_official") / name
                size = archive.stat().st_size
                if size > 32 * 1024 * 1024:
                    raise ValueError(f"Native gem output exceeds the 32 MiB retention bound: {archive}: {size}")
                destination = evidence / name
                shutil.copyfile(archive, destination)
                digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                records.append({"architecture": arch, "configured": configured, "output": str(archive), "bytes": size, "sha256": digest})
                if suffix == "json":
                    profiles[arch, configured] = (size, digest)
                else:
                    payloads[arch, configured] = (size, digest)
    (evidence / "native-gems-record.json").write_text(json.dumps(records, indent=2) + "\n")
    for arch in ("amd64", "arm64"):
        if profiles[arch, False] != profiles[arch, True]:
            raise ValueError(f"{arch} native gem profile differs across configurations")
        if payloads[arch, False] != payloads[arch, True]:
            raise ValueError(f"{arch} native gem tar bytes differ across configurations")
    directory = evidence / "invocation-ruby-native-repeatability"
    paths = [json.loads(line.removeprefix("NATIVE_GEM_BUILD_DIRECTORY "))
             for name in ("stdout.log", "stderr.log")
             for line in (directory / name).read_text().splitlines()
             if line.startswith("NATIVE_GEM_BUILD_DIRECTORY ")]
    if len(paths) != 4 or len({row["directory"] for row in paths}) != 4 or len({row["output"] for row in paths}) != 4:
        raise ValueError("Expected four actual distinct native build paths in action output")
    (evidence / "native-repeatability.json").write_text(json.dumps({"status": "PASS", "payloads": {arch: payloads[arch, False] for arch in ("amd64", "arm64")}}, indent=2) + "\n")
