"""Compare each public release built directly with its transitioned counterpart."""
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tarfile

D = Path(os.environ["RUNNER_TEMP"]) / "chainguard-release"
TARGET = "//debian:gvisor-release-tar-bz2"
ARCHES = {"amd64": "x86_64", "arm64": "aarch64"}


def build():
    # Record each invocation's bytes before another configuration can
    # replace a symlink or materialize another producer at the same path.
    paired = D / "paired"
    paired.mkdir()
    for arch in ARCHES:
        for ext in ("tar.bz2", "runsc", "sentry"):
            name = f"release_{arch}.{ext}"
            shutil.copyfile(Path("bazel-bin/test/rbe/release_prewarmer") / name, paired / name)
    for arch, config in ARCHES.items():
        worker = "//test/rbe/release_prewarmer:" + arch
        platform = "//tools/bazeldefs:linux_amd64" if arch == "amd64" else "@llvm//platforms:linux_aarch64_glibc_static"
        cmd = ["timeout", "--kill-after=30s", "30m", "bazelisk",
               "--bazelrc=" + str(D / "heap.rc"), "--bazelrc=" + str(D / "client.rc"), "build",
               "--config=" + config, "-c", "opt", "--lockfile_mode=off",
               "--remote_download_outputs=toplevel", "--platforms=" + platform,
               "--host_platform=" + worker, "--extra_execution_platforms=" + worker,
               "--extra_toolchains=//test/rbe/go_bootstrap:linux_" + arch + "_ash",
               "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
               "--@rules_python//python/config_settings:bootstrap_impl=native",
               # Keep each direct packaging action independent of local reuse.
               "--modify_execution_info=^PackageTar$=+no-remote-cache,^PackageTar$=+qualification-direct-" + arch,
               "--symlink_prefix=" + str(D / (arch + "-")),
               "--build_event_json_file=" + str(D / (arch + ".bep")),
               "--execution_log_compact_file=" + str(D / (arch + ".compact")),
               "--color=no", "--curses=no", "--noshow_progress", TARGET]
        with (D / (arch + ".log")).open("wb") as out:
            result = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT)
        (D / (arch + ".exit")).write_text(str(result.returncode))
        if result.returncode == 0:
            shutil.copyfile(D / (arch + "-bin") / "debian/gvisor.tar.bz2", D / (arch + ".tar.bz2"))


def file_digest(path):
    with path.open("rb") as reader:
        return hashlib.file_digest(reader, "sha256").hexdigest()


def verify(arch, paired, image):
    row = {"exit": int((D / (arch + ".exit")).read_text()), "target": TARGET}
    with (D / (arch + ".bep")).open() as reader:
        for line in reader:
            event = json.loads(line)
            if "started" in event:
                row["invocation"] = event["started"]["uuid"]
            target = event["id"].get("targetCompleted", {})
            if target.get("label") == TARGET and not target.get("aspect"):
                row["built"] = bool(event.get("completed", {}).get("success"))
                row["configuration"] = target["configuration"]["id"]
    if row["exit"] or not row.get("built"):
        return row
    archive = D / (arch + ".tar.bz2")
    row["sha256"] = file_digest(archive)
    row["archive_equal"] = row["sha256"] == paired["archive"]["sha256"]
    files = {}
    with tarfile.open(archive, "r|bz2") as tar:
        for member in tar:
            if member.isdir():
                continue
            name = member.name.removeprefix("./")
            assert name in paired["files"] and name not in files and member.isreg()
            reader = tar.extractfile(member)
            header = reader.read(64)
            assert header[:6] == b"\x7fELF\x02\x01" and len(header) == 64
            digest = hashlib.sha256(header)
            for chunk in iter(lambda: reader.read(1 << 20), b""):
                digest.update(chunk)
            files[name] = {"sha256": digest.hexdigest(), "bytes": member.size,
                           "mode": member.mode & 0o7777, "e_machine": int.from_bytes(header[18:20], "little")}
    row["files"] = files
    row["matching_files"] = sorted(n for n, data in files.items() if data == paired["files"].get(n))
    args = [str(D / "bb"), "print", "--raw", "--compact_execution_log=" + str(D / (arch + ".compact"))]
    query = """select(.spawn.mnemonic=="PackageTar" and .spawn.targetLabel=="//debian:gvisor-release-tar-bz2") | .spawn |
        [(.runner=="remote"),(.cacheHit//false),(.exitCode//0),((.platform.properties//[])|map({key:.name,value:.value})|
          from_entries|[.Arch,."container-image",."workload-isolation-type"])]"""
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        rows = subprocess.check_output(["jq", "-c", query], stdin=decoder.stdout, text=True)
        decoder.stdout.close()
        assert decoder.wait() == 0
    writers = [json.loads(line) for line in rows.splitlines()]
    row["fresh_native_writer"] = writers == [[True, False, 0, [arch, image, "oci"]]]
    row["verified"] = (row["archive_equal"] and len(row["matching_files"]) == len(paired["files"]) == 9
                       and row["fresh_native_writer"])
    return row


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build()
    else:
        assert sys.argv[1] == "verify" and not (D / "client.rc").exists()
        result_path = D / "result.json"
        result = json.loads(result_path.read_text())
        result["direct"] = {}
        for arch in ARCHES:
            try:
                result["direct"][arch] = verify(arch, result["arms"][arch], result["image"])
            except (OSError, KeyError, ValueError, AssertionError, tarfile.TarError, subprocess.SubprocessError) as error:
                result["direct"][arch] = {"error": type(error).__name__}
        result["direct_match_verified"] = all(row.get("verified", False) for row in result["direct"].values())
        result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
