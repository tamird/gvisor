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
STOCK = "56ffe22724925a43ab36b6f1a72009e0a69e7c81"
COMPILER = "a066005a68e743f3c8bc0d974a2c6c87f414ba18"
PROTO = "@org_golang_google_protobuf//internal/detrand:rand.go"
COMMON = ["--lockfile_mode=off", "--module_mirrors=https://bcr.cloudflaremirrors.com",
          f"--distdir={AREA / 'dist'}", "--color=no", "--curses=no", "--noshow_progress"]
REPORT = {"upstream_control": STOCK, "upstream_artifact": 13929208646, "compiler": COMPILER, "results": {}}


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


def release_analysis(name, cwd, prefix):
    files = run(name, cwd, "cquery", *flags(prefix), "--output=files", prefix + "//:release")
    if files is None:
        return []
    paths = files.splitlines()
    assert all("/release/" in p for p in paths)
    REPORT["results"][name]["outputs"] = paths
    persist()
    return paths


def action(name, cwd, prefix):
    label = prefix + "//:release"
    expr = 'filter(".*//internal/detrand:", deps(' + label + '))'
    result = run(name, cwd, "aquery", *flags(prefix), "--output=text", "--include_commandline=false", expr)
    if result is None:
        return
    blocks = []
    for block in result.split("\n\n"):
        if "internal/detrand/rand.go" not in block:
            continue
        rows = [line.strip() for line in block.splitlines() if
                line.lstrip().startswith(("Target:", "Mnemonic:", "Configuration:"))]
        inputs = sorted(set(re.findall(r"[^\s,\[\]]*internal/detrand/rand\.go", block)))
        blocks.append({"action": rows, "inputs": inputs})
    REPORT["results"][name]["actions_consuming_rand_go"] = blocks
    REPORT["results"][name]["actions_for_matching_owner"] = [
        line.strip() for line in result.splitlines() if line.lstrip().startswith(("Target:", "Mnemonic:"))
    ]
    persist()


PRIOR_FILES = {'containerd-shim-runsc-v1': '006ba28aa6ceea335077f9bf9ded928d6634749eed7d4177676f63bd1b0978a6', 'runsc': '05136831a8e63bb9145247cb8e9590d539aaa8d71b31284d9b3878b838afd551', 'gvisor-bin/checkpointgofer': 'c31a8012d9dd67931d4dff7c52d2e00a7d6cc6ffea3fff555251d60cf42fea58', 'gvisor-bin/runsc-metric-server': '955f50ed0e8b38cea6d729386379e46d00c52b093d071bbf531cf676db5453c3', 'gvisor-bin/runsc-netgofer': 'c7185bface0fd406fd09be0d16d531a9349d91f6387ac63f0730ed8c9d7a1f82', 'gvisor-bin/runsc-fd-parking': '28f5206c05ce4cffb2a7e55090c3ba7e1fbb285aa59b1f6f152939b964e32600', 'gvisor-bin/gvisor-sentry-prewarmer': '5108438e45f485b9843daa27537921cf65ef71d1cc0878fec62cda5507ada09c', 'gvisor-bin/gvisor-proc-umounter': 'c58adb3e05f2d8116a28b4588a17bdb07975b00bdc5a617d14ce08037ab5da36', 'gvisor-bin/gvisor_sentry': '0dd0d69077f9bd18d2edcf0379a87e753ae05d64216ed862978fd40074202c78'}
PRIOR_MODULE = {'original': '684a313c4cca9d1a75e59edd442450344ca84895ecebc3240fa5254341a4fbd7', 'experiment': '7b12c1ff8ac55fcdcc7adff69b2a960c77842198855224944c3baae17e72291f'}
PATCHED = {'sha256': 'eb7bf9d4c9be19de92651a4dc31701c80beef92e04a5c89ab14a1902aa4618f2', 'size': 963, 'reads_executable': False, 'disabled_seed': True}
ADOPTING = 'module(name="downstream_release")\nbazel_dep(name="gvisor",version="0.0.1")\nlocal_path_override(module_name="gvisor",path="/home/runner/work/gvisor/gvisor")\nbazel_dep(name="gazelle",version="0.51.3",repo_name="bazel_gazelle")\nbazel_dep(name="rules_go",version="0.65.0",repo_name="io_bazel_rules_go")\nbazel_dep(name="protobuf",version="35.1",repo_name="com_google_protobuf")\ngo_deps=use_extension("@bazel_gazelle//:extensions.bzl","go_deps")\nuse_repo(go_deps,"org_golang_google_protobuf")\nbazel_dep(name="llvm",version="0.8.24")\nsingle_version_override(\n    module_name = "rules_go",\n    patch_strip = 1,\n    patches = [\n        "//tools:rules_cgo.patch",\n        "//tools:rules_go_export.patch",\n    ],\n    version = "0.65.0",\n)\n\nsingle_version_override(\n    module_name = "google_benchmark",\n    patch_strip = 1,\n    patches = ["//tools:benchmark-umbrella-exports.patch"],\n)\n\nsingle_version_override(\n    module_name = "llvm",\n    patch_strip = 1,\n    patches = ["//tools:llvm-linux-header-default.patch"],\n)\ngo_deps.module_override(\n    patch_strip = 1,\n    patches = ["@gvisor//tools:protobuf.disable-binary-reading-on-init.patch"],\n    path = "google.golang.org/protobuf",\n)\ngo_deps.gazelle_override(\n    directives = [\n        "gazelle:proto disable",\n    ],\n    path = "google.golang.org/grpc",\n)\ngo_deps.gazelle_override(\n    directives = [\n        "gazelle:proto disable",\n    ],\n    path = "github.com/containerd/containerd/v2",\n)\ngo_deps.gazelle_override(\n    directives = [\n        "gazelle:proto disable",\n    ],\n    path = "github.com/containerd/cgroups/v3",\n)\ngo_deps.gazelle_override(\n    directives = [\n        "gazelle:proto disable",\n    ],\n    path = "github.com/containerd/errdefs",\n)\ngo_deps.gazelle_override(\n    directives = [\n        "gazelle:proto disable",\n    ],\n    path = "github.com/containerd/log",\n)\ngo_deps.gazelle_override(\n    directives = [\n        "gazelle:proto disable_global",\n    ],\n    path = "github.com/googleapis/gax-go/v2",\n)\ngo_deps.gazelle_override(\n    directives = [\n        "gazelle:proto disable_global",\n    ],\n    path = "github.com/googleapis/gnostic",\n)\ngo_deps.gazelle_override(\n    directives = [\n        "gazelle:proto disable_global",\n    ],\n    path = "k8s.io/api",\n)\ngo_deps.gazelle_override(\n    directives = [\n        "gazelle:proto disable_global",\n    ],\n    path = "k8s.io/apimachinery",\n)\n\nllvm_headers=use_extension("@llvm//kernel/extension:kernel_headers.bzl","kernel_headers")\nllvm_headers.configure(default_version="6.18")\n'

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
    # Use the transitive canonical Go/protobuf flag labels for release
    # parity. The basic library build needs neither flag nor any direct
    # dependency under those repositories' names in the consumer module.
    regular = ["--platforms=@gvisor//tools/bazeldefs:linux_arm64", "--cpu=aarch64", "-c", "opt", "--nostamp"]
    outputs = output_files("minimal_seqnum", "@gvisor//pkg/tcpip/seqnum:seqnum", regular)
    archives = []
    for output, path in outputs:
        data = path.read_bytes()
        archives.append({"path": output, "size": len(data), "sha256": digest(data),
                         "ar": data.startswith(b"!<arch>\n"), "linux_arm64": b"go object linux arm64 " in data})
    REPORT["seqnum_archives"] = archives
    persist()
    if archives:
        assert any(r["ar"] and r["linux_arm64"] for r in archives), archives
    minimal_flags = [arg.replace("@io_bazel_rules_go//", "@@rules_go+//")
                     .replace("@com_google_protobuf//", "@@protobuf+//") for arg in flags("@gvisor")]
    REPORT["minimal_release_flags"] = minimal_flags
    # Record the actual owner separately even if the nine-file build fails.
    # The canonical external repo was verified by the earlier Go actions.
    global PROTO
    PROTO = "@@gazelle++go_deps+org_golang_google_protobuf//internal/detrand:rand.go"
    defaults = protobuf("minimal_protobuf", CONSUMER)
    plain_files = release("minimal_release", minimal_flags)
    # A successful default release still needs the selected protobuf startup
    # policy assessed against the reference. Do not count an ELF suffix alone.
    if plain_files and defaults == PATCHED and plain_files == PRIOR_FILES:
        REPORT["disposition"] = "Minimal consumer reproduced the complete prior release and protobuf source."
        persist()
        return
    # One remaining control changes only the benchmark public-header override.
    # Root patch labels must remain local to the consuming module.
    benchmark = re.search(r"(?ms)^single_version_override\(\n    module_name = \"google_benchmark\",.*?^\)\n", ADOPTING)
    assert benchmark and "benchmark-umbrella-exports.patch" in benchmark[0]
    adopted = ADOPTING.replace(benchmark[0], "")
    adopted = re.sub(r'local_path_override\(module_name="gvisor",path="[^"]*"\)',
                     lambda _: f'local_path_override(module_name="gvisor",path={json.dumps(str(SOURCE))})', adopted)
    assert adopted.count("single_version_override(") == 2 and adopted.count("go_deps.gazelle_override(") == 9
    assert "benchmark-umbrella-exports.patch" not in adopted
    directory = CONSUMER / "tools"
    directory.mkdir()
    (directory / "BUILD.bazel").write_text('exports_files(glob(["*.patch"]))\n')
    for fname in re.findall(r'"//tools:([^"]+\.patch)"', adopted):
        (directory / fname).write_bytes((SOURCE / "tools" / fname).read_bytes())
    outer(adopted)
    (OUT / "adopting-without-benchmark.MODULE.bazel").write_text(adopted)
    PROTO = "@org_golang_google_protobuf//internal/detrand:rand.go"
    control_proto = protobuf("no_benchmark_protobuf", CONSUMER)
    control_files = release("no_benchmark_release", flags("@gvisor"))
    assert control_proto == PATCHED and control_files, "Missing adopted control or wrong protobuf"
    action("no_benchmark_go_compile", CONSUMER, "@gvisor")
    REPORT["disposition"] = "See emitted ARM64 outputs and source fingerprints for each arm."
    persist()


if __name__ == "__main__":
    main()
