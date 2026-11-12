"""Boot the externally built ARM64 release in a 64 KiB guest."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

def helper(name):
    path = Path(os.environ["RUNNER_TEMP"]) / "paired-stub-guest" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = helper("downstream_release")
guest = helper("paired_stub_guest")

WORK = guest.WORK
HISTORICAL_PROBE_CARRIER = "8d8327953fd0b9b267c14bdb87a1352219749d38"
PROBE = "c49fb618f55e96782877cd132c2bb28a36623f67c2cea77bfc4f192f5320aa22"

PAGE4K_DIGESTS = {
    "containerd-shim-runsc-v1": "378f6d7f81ba24533f0786c8e49d90ea274ed2823202a0c2889bb85f0eb31dc0",
    "gvisor-bin/checkpointgofer": "26572a5f5bf57b98ca96a4eca0e642953c19d6f068c415339b854a543e7cc461",
    "gvisor-bin/gvisor-proc-umounter": "c58adb3e05f2d8116a28b4588a17bdb07975b00bdc5a617d14ce08037ab5da36",
    "gvisor-bin/gvisor-sentry-prewarmer": "5108438e45f485b9843daa27537921cf65ef71d1cc0878fec62cda5507ada09c",
    "gvisor-bin/gvisor_sentry": "2417d9dfc1159cb9ef4e6caa8bea93744a04265351697edc006af96c7c903825",
    "gvisor-bin/runsc-fd-parking": "28f5206c05ce4cffb2a7e55090c3ba7e1fbb285aa59b1f6f152939b964e32600",
    "gvisor-bin/runsc-metric-server": "584a30c6045d133d1ee059aad550ca7c4435058a4ad412898d6effe934ef8b71",
    "gvisor-bin/runsc-netgofer": "c41be8f13824f5ef51093e8281421b5e51994191f45639c5cf38fa27552fba5d",
    "runsc": "245eba16a2e8e77210fa98edfed64abbc9ec7b92734a2ee6620ec5cafd04b1b2"
}


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def prepare_release():
    build.AREA.mkdir()
    build.OUT.mkdir()
    (build.AREA / "dist").symlink_to(WORK / "dist", target_is_directory=True)
    (build.AREA / "bazel").symlink_to(WORK / "bazel9")
    # The authorized Bazel client configuration remains in its original
    # credential-scoped step and is removed by that step's EXIT trap.
    (build.AREA / "client.bazelrc").symlink_to(WORK / "compiler.rc")
    try:
        assemble()
    finally:
        (build.AREA / "client.bazelrc").unlink()


def assemble():
    build.CONSUMER.mkdir()
    (build.CONSUMER / "BUILD.bazel").write_text("")
    caller = ('module(name="downstream_release")\nbazel_dep(name="gvisor",version="0.0.1")\n'
              f'local_path_override(module_name="gvisor",path={json.dumps(str(build.SOURCE))})\n')
    assert build.digest(caller.encode()) == "cb5e18f8ccdf188e39d7d47efd34de667b224fb49e72711e50596f64770b4634"
    build.outer(caller)
    (WORK / "consumer.MODULE.bazel").write_text(caller)
    git("checkout", "--quiet", "--detach", build.COMPILER)
    supplier = build.SOURCE / "MODULE.bazel"
    original = supplier.read_bytes()
    assert build.digest(original) == build.PRIOR_MODULE["original"]
    header = 'kernel_headers.configure(default_version = "6.18")\n'
    assert original.decode().count(header) == 1
    edited = original.decode().replace(header, "").replace('use_repo(kernel_headers, "kernel_headers")',
        'use_repo(kernel_headers, "kernel_headers")\n'
        'root_kernel_headers = use_extension("@llvm//kernel/extension:kernel_headers.bzl", "kernel_headers", dev_dependency = True)\n'
        'root_kernel_headers.configure(default_version = "6.18")')
    start = edited.index("go_deps.module_override(")
    tags = edited[start:]
    assert tags.count("go_deps.module_override(") == 1 and tags.count("go_deps.gazelle_override(") == 9
    supplier.write_text(edited[:start] + 'root_go_deps = use_extension("@bazel_gazelle//:extensions.bzl", "go_deps", dev_dependency = True)\n'
                      + tags.replace("go_deps.", "root_go_deps."))
    assert build.digest(supplier.read_bytes()) == build.PRIOR_MODULE["experiment"]
    patch = WORK / "pr16098.patch"
    assert build.digest(patch.read_bytes()) == build.INTEGRATION["patch_sha256"]
    git("apply", "--check", str(patch))
    git("apply", str(patch))
    for path, digest in build.INTEGRATION["blobs"].items():
        assert git("hash-object", path) == digest
    flags = [arg.replace("@io_bazel_rules_go//", "@@rules_go+//")
             .replace("@com_google_protobuf//", "@@protobuf+//") for arg in build.flags("@gvisor")]
    flags.append("--@gvisor//tools/bazeldefs:page_size=64k")
    override, = build.selection("after_2513")
    build.COMMON.append(override)
    override_dir = Path(override.split("=", 2)[2])
    defaults = (override_dir / "internal/bzlmod/default_gazelle_overrides.bzl").read_bytes()
    assert build.digest(defaults) == build.DEFAULT_SHA["after_2513"]
    records = {}
    inputs = WORK / "inputs"
    inputs.mkdir()
    record = {"source": build.COMPILER, "integrated_pr": build.INTEGRATION, "reference_artifact": 13959154334,
              "original_release_invocation": "9d4a78c0-894d-4239-980f-23ff1a03c5b8",
              "consumer_sha256": build.digest(caller.encode()), "gazelle_selection": build.UPSTREAM["after_2513"],
              "defaults_sha256": build.digest(defaults), "members": records,
              "supplier_module_sha256": build.digest(supplier.read_bytes()), "attempts": {}}
    guest.save("release", record)
    target = "@gvisor//:release"
    for name, extra in (("cache_only", ["--experimental_remote_require_cached"]), ("rebuild", [])):
        bep = WORK / (name + ".bep")
        result = build.run(name, build.CONSUMER, "build", *flags, *extra,
                           "--build_event_json_file=" + str(bep), target)
        summary = {"exit": build.REPORT["results"][name]["exit"]}
        for line in bep.open():
            event = json.loads(line)
            if "started" in event:
                summary["invocation"] = event["started"].get("uuid")
            if "buildMetrics" in event:
                summary["runners"] = event["buildMetrics"].get("actionSummary", {}).get("runnerCount", [])
        record["attempts"][name] = summary
        guest.save("release", record)
        if result is not None:
            record["acquisition"] = name
            break
    assert "acquisition" in record, record["attempts"]
    paths = build.run("release_files", build.CONSUMER, "cquery", *flags, "--output=files", target)
    assert paths
    execution_root = Path(subprocess.check_output([
        str(build.AREA / "bazel"), "--nosystem_rc", "--nohome_rc", "--noworkspace_rc",
        f"--bazelrc={build.AREA / 'client.bazelrc'}", "info", "execution_root",
    ], cwd=build.CONSUMER, text=True).strip())
    outputs = [(name, execution_root / name) for name in paths.splitlines()]
    for output, path in outputs:
        name = output.split("/release/")[-1]
        with path.open("rb") as stream:
            prefix = stream.read(20)
            assert prefix[:6] == b"\x7fELF\x02\x01" and int.from_bytes(prefix[18:20], "little") == 183
            stream.seek(0)
            sha256 = hashlib.sha256()
            while chunk := stream.read(1 << 20):
                sha256.update(chunk)
            digest = sha256.hexdigest()
        records[name] = {"sha256": digest, "size": path.stat().st_size}
        guest.save("release", record)
        assert name in PAGE4K_DIGESTS and not (inputs / "release" / name).exists(), name
        dest = inputs / "release" / name
        dest.parent.mkdir(exist_ok=True, parents=True)
        shutil.copyfile(path, dest)
        dest.chmod(0o755)
    assert records.keys() == PAGE4K_DIGESTS.keys()
    record["different_from_4k"] = sorted(name for name in records if records[name]["sha256"] != PAGE4K_DIGESTS[name])
    guest.save("release", record)
    (WORK / "release-manifest.sha256").write_text("".join(
        f"{records[name]['sha256']}  {name}\n" for name in sorted(records)))
    archive = WORK / "downstream-release-arm64.tar.gz"
    subprocess.run(["tar", "-C", str(inputs / "release"), "--sort=name", "--mtime=@0", "--owner=0",
                    "--group=0", "--numeric-owner", "-czf", str(archive), *sorted(PAGE4K_DIGESTS)], check=True)
    with archive.open("rb") as stream:
        digest = hashlib.sha256()
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    record["release_archive_sha256"] = digest.hexdigest()
    record["release_verified"] = True
    guest.save("release", record)
    git("apply", "-R", str(patch))
    supplier.write_bytes(original)
    assert not git("diff", "--name-only")
    # Only the standalone probe is built on its original fixture source.
    # All runtime executables above came from the external gVisor module.
    git("checkout", "--quiet", "--detach", HISTORICAL_PROBE_CARRIER)
    assert git("rev-parse", "HEAD") == HISTORICAL_PROBE_CARRIER


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
    expected = {"release/" + name: item["sha256"] for name, item in record["members"].items()}
    expected["probe"] = PROBE
    assert manifest.read_text() == "".join(f"{expected[name]}  {name}\n" for name in sorted(expected))
    record["verified"] = True
    guest.save("release", record)


def inputs_check():
    target = "//test/rbe/paired_stub_guest:page64k"
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
        guest.SOURCE = build.COMPILER
        guest.GUESTS = {"page64k": guest.GUESTS["page64k"]}
        raise SystemExit(guest.collect())
    else:
        raise ValueError(command)
