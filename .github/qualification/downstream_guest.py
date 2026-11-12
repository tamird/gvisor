"""Build the published ARM64 release archive and exercise its packaged Sentry."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

WORK = Path(os.environ["RUNNER_TEMP"]) / "paired-stub-guest"
SOURCE = "48f457b4e040312422b8e28b97081998d1d81297"
HISTORICAL_PROBE_CARRIER = "8d8327953fd0b9b267c14bdb87a1352219749d38"
PROBE = "c49fb618f55e96782877cd132c2bb28a36623f67c2cea77bfc4f192f5320aa22"

# -I excludes the script directory. Load only the fixture pinned by the workflow.
spec = importlib.util.spec_from_file_location("paired_stub_guest", WORK / "paired_stub_guest.py")
assert spec is not None and spec.loader is not None
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def elf_machine(data: bytes) -> int:
    assert data[:6] == b"\x7fELF\x02\x01", data[:6]
    return int.from_bytes(data[18:20], "little")


def build_receipt(name: str, flags: list[str], targets: list[str]) -> dict:
    bep = WORK / (name + ".bep")
    result = subprocess.run([*guest.BAZEL, "build", *flags, "--build_event_json_file=" + str(bep), *targets])
    receipt = {"exit": result.returncode, "targets": targets}
    with bep.open() as events:
        for line in events:
            event = json.loads(line)
            if "started" in event:
                receipt["invocation"] = event["started"]["uuid"]
            if "buildMetrics" in event:
                receipt["runners"] = event["buildMetrics"].get("actionSummary", {}).get("runnerCount", [])
    guest.save(name, receipt)
    result.check_returncode()
    return receipt


def prepare_release() -> None:
    git("checkout", "--quiet", "--detach", SOURCE)
    assert git("rev-parse", "HEAD") == SOURCE
    assert 'bazel_dep(name = "llvm", version = "0.8.26")' in Path("MODULE.bazel").read_text()
    assert os.uname().machine == "x86_64"
    receipt = build_receipt("cross_build", guest.BUILD_FLAGS, ["//debian:gvisor-release-tar-bz2"])
    archive = WORK / "arm64-gvisor.tar.bz2"
    shutil.copyfile("bazel-bin/debian/gvisor.tar.bz2", archive)
    inputs = WORK / "inputs" / "release"
    members = {}
    with tarfile.open(archive, "r:bz2") as tar:
        for entry in tar:
            if entry.isdir():
                assert entry.name in ("gvisor-bin", "./gvisor-bin"), entry.name
                continue
            name = entry.name.removeprefix("./")
            assert entry.isfile() and name in guest.MEMBERS and name not in members, entry.name
            assert entry.mode & 0o111, name
            with tar.extractfile(entry) as source:
                content = source.read()
            assert elf_machine(content) == 183, name
            output = inputs / name
            output.parent.mkdir(exist_ok=True, parents=True)
            output.write_bytes(content)
            output.chmod(0o755)
            members[name] = {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
    assert set(members) == guest.MEMBERS, members.keys()
    (WORK / "release-manifest.sha256").write_text("".join(f"{members[k]['sha256']}  {k}\n" for k in sorted(members)))
    record = {"source": SOURCE, "members": members, "release_verified": True,
              "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "arm64": receipt}
    guest.save("release", record)
    flags = ["--config=x86_64" if arg == "--config=aarch64" else arg for arg in guest.BUILD_FLAGS]
    flags.append("--custom_malloc=@com_google_absl//absl/base:malloc_internal")
    sidecars = ["//runsc/fdparking:fdparking_binary", "//runsc/prewarmer:prewarmer_binary",
                "//runsc/procumounter:procumounter_binary"]
    record["amd64"] = build_receipt("amd64", flags, sidecars)
    paths = subprocess.check_output([*guest.BAZEL, "cquery", *flags, "--output=files",
                                     "set(" + " ".join(sidecars) + ")"], text=True).splitlines()
    assert len(paths) == len(set(paths)) == 3, paths
    root = Path(subprocess.check_output([*guest.BAZEL, "info", "execution_root"], text=True).strip())
    for path in paths:
        assert elf_machine((root / path).read_bytes()) == 62, path
    record["amd64"]["elf_files"] = paths
    guest.save("release", record)
    git("checkout", "--quiet", "--detach", HISTORICAL_PROBE_CARRIER)


def prepare_probe():
    record = json.loads((WORK / "release.json").read_bytes())
    assert record["release_verified"] and git("rev-parse", "HEAD") == HISTORICAL_PROBE_CARRIER
    target = "//test/rbe/paired_stub_guest:pagesize_probe"
    options = [*guest.BUILD_FLAGS, "--platforms=@llvm//platforms:linux_aarch64_musl"]
    subprocess.run([*guest.BAZEL, "build", *options, target], check=True)
    probe = Path("bazel-bin/test/rbe/paired_stub_guest/pagesize_probe")
    assert guest.sha(probe.read_bytes()) == PROBE
    inputs = WORK / "inputs"
    shutil.copyfile(probe, inputs / "probe")
    (inputs / "probe").chmod(0o755)
    manifest = inputs / "manifest.sha256"
    manifest.write_text("".join(f"{guest.sha(p.read_bytes())}  {p.relative_to(inputs)}\n"
                                for p in sorted(inputs.rglob("*")) if p.is_file()))
    record.update(probe_sha256=PROBE, probe_original_artifact=13956109226,
                  probe_platform="@llvm//platforms:linux_aarch64_musl",
                  manifest_sha256=guest.sha(manifest.read_bytes()))
    guest.save("release", record)
    record["verified"] = True
    guest.save("release", record)


def inputs_check():
    target = "//test/rbe/paired_stub_guest:page4k"
    report = {}
    for line in (WORK / "guest-inputs.bep").open():
        event = json.loads(line)
        if "started" in event:
            report["invocation"] = event["started"]["uuid"]
        assert "testResult" not in event
        ident = event["id"].get("targetCompleted", {})
        if ident.get("label") == target and not ident.get("aspect"):
            completed = event.get("completed", {})
            groups = [o for o in completed.get("outputGroup", []) if o["name"] == "guest_inputs"]
            report[target] = bool(completed.get("success") and len(groups) == 1 and groups[0].get("fileSets"))
    guest.save("guest-inputs", report)
    assert report.get(target)


if __name__ == "__main__":
    command, = sys.argv[1:]
    if command == "release":
        prepare_release()
    elif command == "probe":
        prepare_probe()
    elif command == "check":
        inputs_check()
    elif command == "collect":
        guest.SOURCE = SOURCE
        guest.GUESTS = {"page4k": guest.GUESTS["page4k"]}
        raise SystemExit(guest.collect())
    else:
        raise ValueError(command)
