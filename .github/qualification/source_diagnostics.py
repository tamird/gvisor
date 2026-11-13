"""Build the four previously unbuilt LLVM coverage targets and inspect their outputs."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(os.environ["RUNNER_TEMP"]) / "llvm-source-presubmit"
SOURCE = "9513007900510997986be1562eec5e54c4e206fc"
TARGETS = (
    "//runsc:runsc_coverage",
    "//runsc:runsc_race_coverage",
    "//runsc/cmd/sentry:gvisor_sentry_coverage",
    "//runsc/cmd/sentry:gvisor_sentry_race_coverage",
)
EXCLUDED = re.compile(rb"/pkg/(coverage|ring0|sleep|sync|syncevent|bpf|sentry/platform)/")


def check_output(arch, label, path):
    with path.open("rb") as binary:
        header = binary.read(64)
        binary.seek(0)
        digest = hashlib.file_digest(binary, "sha256").hexdigest()
    machine = int.from_bytes(header[18:20], "little")
    programs = subprocess.check_output(["readelf", "-lW", path], text=True)
    result = {"path": str(path), "bytes": path.stat().st_size, "sha256": digest, "elf_machine": machine,
              "static": "INTERP" not in programs}
    assert header[:6] == b"\x7fELF\x02\x01" and machine == {"amd64": 62, "arm64": 183}[arch], result
    assert result["static"], result
    cmd = [str(path.resolve())] if arch == "amd64" else ["qemu-aarch64", str(path.resolve())]
    version = subprocess.run([*cmd, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=70, text=True)
    result["version"] = version.stdout[:600]
    result["version_exit"] = version.returncode
    if "gvisor_sentry" in label:
        # Match the owning PR's readback of KCOV positions and package exclusions.
        symbols = subprocess.run([*cmd, "symbolize", "-all"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=150)
        result["symbolize_exit"] = symbols.returncode
        result["symbolize_stderr"] = symbols.stderr.decode(errors="replace")[:300]
        result["kernel_positions"] = sum(b"pkg/sentry/kernel/" in l for l in symbols.stdout.splitlines())
        result["excluded_positions"] = sum(bool(EXCLUDED.search(l)) for l in symbols.stdout.splitlines())
    return result


def run(arch):
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    assert subprocess.check_output(["uname", "-m"], text=True).strip() == "x86_64"
    bazel = [str(ROOT / "bazel"), "--host_jvm_args=-Xmx7g", "--nosystem_rc", "--nohome_rc",
             "--bazelrc=" + str(ROOT / "remote.rc")]
    flags = ["--config=" + {"amd64": "x86_64", "arm64": "aarch64"}[arch],
             "--host_platform=//tools/bazeldefs:linux_amd64",
             "--extra_execution_platforms=//tools/bazeldefs:linux_amd64",
             "--remote_default_exec_properties=Arch=amd64", "--distdir=" + str(ROOT / "dist"),
             "--jobs=60", "--keep_going", "--strip=never", "--remote_download_outputs=toplevel"]
    receipt = {"source": SOURCE, "target_arch": arch, "client": "amd64", "outputs": {}, "targets": {}}
    file = ROOT / "results" / (arch + ".json")
    def save():
        file.write_text(json.dumps(receipt, indent=2) + "\n")
    bep = ROOT / (arch + ".bep")
    log = ROOT / (arch + ".log")
    with log.open("w") as out:
        receipt["exit"] = subprocess.run([*bazel, "build", *flags, "--build_event_json_file=" + str(bep),
                                          *TARGETS], stdout=out, stderr=subprocess.STDOUT).returncode
    for line in bep.open():
        event = json.loads(line)
        if "started" in event:
            receipt["invocation"] = event["started"]["uuid"]
        key = event["id"].get("targetCompleted", {})
        if key.get("label") in TARGETS and not key.get("aspect"):
            receipt["targets"][key["label"]] = event.get("completed", {}).get("success", False)
    receipt["errors"] = [line.strip()[:650] for line in log.open(errors="replace") if (
        line.startswith("ERROR:") or "undefined reference" in line or "cannot find " in line or "fatal error:" in line
    ) and not re.search("api.key|authorization|credential|bearer|remote_header|bes_header", line, re.I)][:25]
    save()
    good = receipt["exit"] == 0 and set(receipt["targets"]) == set(TARGETS)
    for label in TARGETS:
        if not receipt["targets"].get(label):
            good = False
            continue
        lines = subprocess.check_output([*bazel, "cquery", *flags, "--output=files", label], text=True).splitlines()
        (path,) = map(Path, lines)
        try:
            item = check_output(arch, label, path)
        except (OSError, AssertionError, subprocess.SubprocessError) as err:
            item = {"error": str(err)[:500]}
        receipt["outputs"][label] = item
        good &= item.get("version_exit") == 0 and "runsc version" in item.get("version", "")
        if "gvisor_sentry" in label:
            good &= item.get("symbolize_exit") == 0 and item.get("kernel_positions", 0) > 0 and item.get("excluded_positions") == 0
        save()
    receipt["verified"] = good
    save()
    print(arch, receipt.get("invocation"), receipt["exit"], receipt["targets"], receipt["errors"], good, flush=True)
    return good


if __name__ == "__main__":
    sys.exit(not run(sys.argv[1]))
