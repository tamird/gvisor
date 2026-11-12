"""Check the public ARM64 release fileset from an independent Bazel module."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

SOURCE = Path.cwd()
AREA = Path(os.environ["RUNNER_TEMP"]) / "downstream-release"
OUT = AREA / "results"
CONSUMER = AREA / "consumer"
COMPILER = "a066005a68e743f3c8bc0d974a2c6c87f414ba18"
PROTO = "@org_golang_google_protobuf//internal/detrand:rand.go"
COMMON = ["--lockfile_mode=off", "--module_mirrors=https://bcr.cloudflaremirrors.com",
          f"--distdir={AREA / 'dist'}", "--color=no", "--curses=no", "--noshow_progress"]
REPORT = {"compiler": COMPILER, "results": {}}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def persist():
    (OUT / "report.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def run(name, cwd, cmd, *args):
    bazel = [str(AREA / "bazel"), "--nosystem_rc", "--nohome_rc", "--noworkspace_rc",
             f"--bazelrc={AREA / 'client.bazelrc'}", cmd]
    with (AREA / (name + ".stdout")).open("w") as stdout, (AREA / (name + ".stderr")).open("w") as stderr:
        code = subprocess.run(bazel + COMMON + list(args), cwd=cwd, stdout=stdout, stderr=stderr).returncode
    data = (AREA / (name + ".stderr")).read_text(errors="replace")
    marker = re.compile(r"error|failed|non-root|no such|not visible|not defined|not found|incompatible|cannot|not generated|no matching|invocation id", re.I)
    diagnostics = [line[:900] for line in data.splitlines() if marker.search(line) and
                   not re.search(r"remote_header|authorization|bearer |x-buildbuddy-api-key|bes_header", line, re.I)]
    REPORT["results"][name] = {"exit": code, "diagnostics": diagnostics[:28]}
    persist()
    print(name, code, "\n".join(diagnostics[:3]), flush=True)
    return (AREA / (name + ".stdout")).read_text() if code == 0 else None


def outer(text):
    (CONSUMER / "MODULE.bazel").write_text(text)


def protobuf(name, cwd):
    result = run(name, cwd, "query", "--output=location", PROTO)
    if result is None:
        return None
    match = re.search(r"^(.*?):\d+:\d+: ", result, re.M)
    assert match, result
    path = cwd / Path(match[1]).with_name("rand.go")
    data = path.read_bytes()
    record = {"sha256": digest(data), "size": len(data),
              "reads_executable": b"os.Executable()" in data and b"os.Open(" in data,
              "disabled_seed": b"var randSeed uint64" in data}
    REPORT["results"][name]["source"] = record
    persist()
    return record


def flags(prefix):
    return [f"--platforms={prefix}//tools/bazeldefs:linux_arm64", "--cpu=aarch64", "-c", "opt",
            "--nostamp", "--conlyopt=-std=gnu17", "--cxxopt=-std=c++17", "--host_conlyopt=-std=gnu17",
            "--host_cxxopt=-std=c++17", "--features=system_include_paths", "--host_features=system_include_paths",
            "--@io_bazel_rules_go//go/config:pure", "--@com_google_protobuf//bazel/toolchains:prefer_prebuilt_protoc",
            "--incompatible_enable_proto_toolchain_resolution=true"]


PRIOR_FILES = {'containerd-shim-runsc-v1': '006ba28aa6ceea335077f9bf9ded928d6634749eed7d4177676f63bd1b0978a6', 'runsc': '05136831a8e63bb9145247cb8e9590d539aaa8d71b31284d9b3878b838afd551', 'gvisor-bin/checkpointgofer': 'c31a8012d9dd67931d4dff7c52d2e00a7d6cc6ffea3fff555251d60cf42fea58', 'gvisor-bin/runsc-metric-server': '955f50ed0e8b38cea6d729386379e46d00c52b093d071bbf531cf676db5453c3', 'gvisor-bin/runsc-netgofer': 'c7185bface0fd406fd09be0d16d531a9349d91f6387ac63f0730ed8c9d7a1f82', 'gvisor-bin/runsc-fd-parking': '28f5206c05ce4cffb2a7e55090c3ba7e1fbb285aa59b1f6f152939b964e32600', 'gvisor-bin/gvisor-sentry-prewarmer': '5108438e45f485b9843daa27537921cf65ef71d1cc0878fec62cda5507ada09c', 'gvisor-bin/gvisor-proc-umounter': 'c58adb3e05f2d8116a28b4588a17bdb07975b00bdc5a617d14ce08037ab5da36', 'gvisor-bin/gvisor_sentry': '0dd0d69077f9bd18d2edcf0379a87e753ae05d64216ed862978fd40074202c78'}
PRIOR_MODULE = {'original': '684a313c4cca9d1a75e59edd442450344ca84895ecebc3240fa5254341a4fbd7', 'experiment': '7b12c1ff8ac55fcdcc7adff69b2a960c77842198855224944c3baae17e72291f'}

def output_files(name, target, options):
    built = run(name + "_build", CONSUMER, "build", *options, target)
    if built is None:
        return []
    files = run(name + "_files", CONSUMER, "cquery", *options, "--output=files", target)
    assert files, "Successful build without listed outputs"
    root = Path(subprocess.check_output([str(AREA / "bazel"), "--nosystem_rc", "--nohome_rc",
        "--noworkspace_rc", f"--bazelrc={AREA / 'client.bazelrc'}", "info", "execution_root"],
        cwd=CONSUMER, text=True).strip())
    return [(path, root / path) for path in files.splitlines()]


def release(name, options):
    target = "@gvisor//:release"
    records = {}
    for output, path in output_files(name, target, options):
        with path.open("rb") as stream:
            prefix = stream.read(20)
            assert prefix[:6] == b"\x7fELF\x02\x01" and int.from_bytes(prefix[18:20], "little") == 183
            stream.seek(0)
            records[output.split("/release/")[-1]] = hashlib.file_digest(stream, "sha256").hexdigest()
    if records:
        assert records.keys() == PRIOR_FILES.keys()
        REPORT[name] = {"files": records, "identical": sorted(k for k in records if records[k] == PRIOR_FILES[k]),
                        "different": sorted(k for k in records if records[k] != PRIOR_FILES[k])}
        persist()
    return records



CGROUPS = "@@gazelle++go_deps+com_github_containerd_cgroups_v3"
CGROUP2 = CGROUPS + "//cgroup2:cgroup2"


def compiler_errors(name):
    # Go diagnostics include source positions even when the surrounding
    # Bazel wrapper reports only the package action and the exit status.
    lines = (AREA / (name + ".stderr")).read_text(errors="replace").splitlines()
    selected = [line.strip()[:1200] for line in lines
                if re.match(r"^\s*(?:[^ :]+/)*[^ :]+\.go:\d+(?::\d+)?: ", line)
                and not re.search(r"remote_header|authorization|bearer |x-buildbuddy-api-key|bes_header", line, re.I)]
    REPORT["results"][name]["go_diagnostics"] = selected[:40]
    persist()
    for line in selected[:3]:
        print(name, line, flush=True)
    return selected


def stats_inputs(name):
    target = CGROUPS + "//cgroup2/stats:all"
    owners = run(name + "_owners", CONSUMER, "query", "--output=label_kind", target)
    assert owners, "Missing cgroup2/stats metadata"
    results = []
    for line in owners.splitlines():
        kind, _, label = line.split(maxsplit=2)
        if kind not in ("go_library", "go_proto_library", "proto_library"):
            continue
        inputs = run(name + "_" + label.split(":")[-1], CONSUMER, "query",
                     "labels(srcs, " + label + ")") if kind != "go_proto_library" else ""
        assert inputs is not None
        results.append({"rule": line, "srcs": sorted(inputs.splitlines())})
    REPORT[name] = results
    persist()
    assert results, "Missing underlying rules"

def main():
    CONSUMER.mkdir()
    (CONSUMER / "BUILD.bazel").write_text("")
    minimal = ('module(name="downstream_release")\nbazel_dep(name="gvisor",version="0.0.1")\n'
               f'local_path_override(module_name="gvisor",path={json.dumps(str(SOURCE))})\n')
    outer(minimal)
    (OUT / "minimal.MODULE.bazel").write_text(minimal)
    subprocess.run(["git", "checkout", "--quiet", "--detach", COMPILER], check=True)
    module = SOURCE / "MODULE.bazel"
    original = module.read_text()
    header = 'kernel_headers.configure(default_version = "6.18")\n'
    assert original.count(header) == 1
    edited = original.replace(header, "").replace('use_repo(kernel_headers, "kernel_headers")',
        'use_repo(kernel_headers, "kernel_headers")\n'
        'root_kernel_headers = use_extension("@llvm//kernel/extension:kernel_headers.bzl", "kernel_headers", dev_dependency = True)\n'
        'root_kernel_headers.configure(default_version = "6.18")')
    start = edited.index("go_deps.module_override(")
    tags = edited[start:]
    assert tags.count("go_deps.module_override(") == 1 and tags.count("go_deps.gazelle_override(") == 9
    module.write_text(edited[:start] + 'root_go_deps = use_extension("@bazel_gazelle//:extensions.bzl", "go_deps", dev_dependency = True)\n'
                      + tags.replace("go_deps.", "root_go_deps."))
    REPORT["supplier_module"] = {"original": digest(original.encode()), "experiment": digest(module.read_bytes())}
    assert REPORT["supplier_module"] == PRIOR_MODULE
    persist()
    options = [arg.replace("@io_bazel_rules_go//", "@@rules_go+//")
               .replace("@com_google_protobuf//", "@@protobuf+//") for arg in flags("@gvisor")]
    REPORT["release_flags"] = options
    # Replay the same release invocation, including its unconfigured root.
    bare_files = release("bare_release", options)
    bare_errors = compiler_errors("bare_release_build")
    caller = minimal + ('bazel_dep(name="gazelle",version="0.51.3",repo_name="bazel_gazelle")\n'
                        'go_deps = use_extension("@bazel_gazelle//:extensions.bzl", "go_deps")\n')
    directive = ('go_deps.gazelle_override(directives=["gazelle:proto disable"],'
                 'path="github.com/containerd/cgroups/v3")\n')
    for case, text in (("no_tag", caller), ("cgroups_tag", caller + directive)):
        outer(text)
        (OUT / (case + ".MODULE.bazel")).write_text(text)
        stats_inputs(case + "_cgroup2_stats")
        built = run(case, CONSUMER, "build", *options, CGROUP2)
        errors = compiler_errors(case)
        if built is None:
            assert errors, "Direct control has no source-positioned Go error: " + case
    if REPORT["results"]["cgroups_tag"]["exit"] == 0:
        global PROTO
        PROTO = "@@gazelle++go_deps+org_golang_google_protobuf//internal/detrand:rand.go"
        selected = protobuf("cgroups_only_protobuf", CONSUMER)
        assert selected and selected["size"] == 1683, selected
        tagged_files = release("cgroups_only_release", options)
        next_errors = compiler_errors("cgroups_only_release_build")
        if not tagged_files:
            diagnostics = REPORT["results"]["cgroups_only_release_build"]["diagnostics"]
            assert next_errors or any(line.startswith("ERROR:") for line in diagnostics), "Missing release diagnostic"
    if not bare_files:
        assert bare_errors, "Bare release ended without a source-positioned Go diagnostic"
        REPORT["bare_failed_at_original_package"] = any("com_github_containerd_cgroups_v3/cgroup2/" in line for line in bare_errors)
    REPORT["prior_artifact"] = 13953442966
    persist()


if __name__ == "__main__":
    main()
