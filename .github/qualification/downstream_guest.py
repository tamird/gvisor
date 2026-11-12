"""Boot the externally built ARM64 release with the established 4K guest."""
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
EXPECTED_MANIFEST = "fb17eda0113026401d58398008092fe5da4561a4d40a447a09e5c0b3374df83c"

EXPECTED = {'containerd-shim-runsc-v1': 'f7619c06249fd9ee5f41fdbd85fc632a4edbdcad78a500e20da15887902ea6a3', 'runsc': 'ee3551b03eb1c342ef4628da606ccfd3093d4d1beb85cfb9eb2b7a88e37e575f', 'gvisor-bin/checkpointgofer': '90763d17a2164247ba63014c6cd85706c46318cb9cc2d00653f41158e57c5129', 'gvisor-bin/runsc-metric-server': '815b0a68d5c70a2e1f91ab2af42858751cad390daf20b9c60d83b59787f78df6', 'gvisor-bin/runsc-netgofer': '09cd2cca1a4eaf319977ef137fc819e77046b257dd998338de7cb477ec261f35', 'gvisor-bin/runsc-fd-parking': '28f5206c05ce4cffb2a7e55090c3ba7e1fbb285aa59b1f6f152939b964e32600', 'gvisor-bin/gvisor-sentry-prewarmer': '5108438e45f485b9843daa27537921cf65ef71d1cc0878fec62cda5507ada09c', 'gvisor-bin/gvisor-proc-umounter': 'c58adb3e05f2d8116a28b4588a17bdb07975b00bdc5a617d14ce08037ab5da36', 'gvisor-bin/gvisor_sentry': '45b67547f22552a5e7c0439953339a118bc5b7b0f939d16b8a7bf83cf819250c'}


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
    (build.CONSUMER / "BUILD.bazel").write_text('exports_files(["rules_go_export.patch"])\n')
    shutil.copyfile(build.SOURCE / "tools/rules_go_export.patch", build.CONSUMER / "rules_go_export.patch")
    caller = ('module(name="downstream_release")\nbazel_dep(name="gvisor",version="0.0.1")\n'
              f'local_path_override(module_name="gvisor",path={json.dumps(str(build.SOURCE))})\n'
              'bazel_dep(name="gazelle",version="0.51.3",repo_name="bazel_gazelle")\n'
              'go_deps = use_extension("@bazel_gazelle//:extensions.bzl", "go_deps")\n'
              'go_deps.gazelle_override(directives=["gazelle:proto disable"],'
              'path="github.com/containerd/cgroups/v3")\n')
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
    outputs = build.output_files("guest_downstream_release", "@gvisor//:release", flags)
    records = {}
    inputs = WORK / "inputs"
    inputs.mkdir()
    record = {"source": build.COMPILER, "integrated_pr": build.INTEGRATION, "reference_artifact": 13959050709,
              "members": records, "supplier_module_sha256": build.digest(supplier.read_bytes())}
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
        assert digest == EXPECTED[name], name
        dest = inputs / "release" / name
        dest.parent.mkdir(exist_ok=True, parents=True)
        shutil.copyfile(path, dest)
        dest.chmod(0o755)
    assert records.keys() == EXPECTED.keys()
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
    assert record["manifest_sha256"] == EXPECTED_MANIFEST
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
        guest.SOURCE = build.COMPILER
        guest.GUESTS = {"page4k": guest.GUESTS["page4k"]}
        raise SystemExit(guest.collect())
    else:
        raise ValueError(command)
