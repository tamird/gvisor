"""Check the public ARM64 release fileset from an independent Bazel module."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import urllib.request
import xml.etree.ElementTree as ET

SOURCE = Path.cwd()
AREA = Path(os.environ["RUNNER_TEMP"]) / "downstream-release"
OUT = AREA / "results"
CONSUMER = AREA / "consumer"
COMPILER = "a066005a68e743f3c8bc0d974a2c6c87f414ba18"
CGROUPS = "@@gazelle++go_deps+com_github_containerd_cgroups_v3"
GAZELLE = "@@gazelle+"
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


def flags(prefix):
    return [f"--platforms={prefix}//tools/bazeldefs:linux_arm64", "--cpu=aarch64", "-c", "opt",
            "--nostamp", "--conlyopt=-std=gnu17", "--cxxopt=-std=c++17", "--host_conlyopt=-std=gnu17",
            "--host_cxxopt=-std=c++17", "--features=system_include_paths", "--host_features=system_include_paths",
            "--@io_bazel_rules_go//go/config:pure", "--@com_google_protobuf//bazel/toolchains:prefer_prebuilt_protoc",
            "--incompatible_enable_proto_toolchain_resolution=true"]


PRIOR_FILES = {"containerd-shim-runsc-v1": "f7619c06249fd9ee5f41fdbd85fc632a4edbdcad78a500e20da15887902ea6a3", "gvisor-bin/checkpointgofer": "90763d17a2164247ba63014c6cd85706c46318cb9cc2d00653f41158e57c5129", "gvisor-bin/gvisor-proc-umounter": "c58adb3e05f2d8116a28b4588a17bdb07975b00bdc5a617d14ce08037ab5da36", "gvisor-bin/gvisor-sentry-prewarmer": "5108438e45f485b9843daa27537921cf65ef71d1cc0878fec62cda5507ada09c", "gvisor-bin/gvisor_sentry": "45b67547f22552a5e7c0439953339a118bc5b7b0f939d16b8a7bf83cf819250c", "gvisor-bin/runsc-fd-parking": "28f5206c05ce4cffb2a7e55090c3ba7e1fbb285aa59b1f6f152939b964e32600", "gvisor-bin/runsc-metric-server": "815b0a68d5c70a2e1f91ab2af42858751cad390daf20b9c60d83b59787f78df6", "gvisor-bin/runsc-netgofer": "09cd2cca1a4eaf319977ef137fc819e77046b257dd998338de7cb477ec261f35", "runsc": "ee3551b03eb1c342ef4628da606ccfd3093d4d1beb85cfb9eb2b7a88e37e575f"}
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



INTEGRATION = {'base': 'a066005a68e743f3c8bc0d974a2c6c87f414ba18', 'pr': '66eb2774175258362fb3afaa11d75dec66c3684a', 'integrated_tree': 'a8f2cacb450dff6c03c6e5b432210c7a8c6d547c', 'patch_sha256': '200f2befd976ffb04623281d62f510340ce50fe3354b7c1e85d0c3dfd4a1a88a', 'blobs': {'nogo.yaml': '5adebdb80aa6b2134d1bcd9acd4c348921ee3769', 'tools/bazeldefs/go.bzl': '8fbe4f67166d47d5e748236318093fe62e9e9f35', 'tools/nogo/README.md': 'd49294d7975e6ca1c6aaf0e468e56199fb483752', 'tools/nogo/check/BUILD': 'b9556912b9a176226209719a4b6bed0b54c6ba7b', 'tools/nogo/check/check.go': '1457d40647344e71e51129d5f81f0c64894c07e0', 'tools/nogo/check/check_test.go': '919abec20d99bb7eda692f67d7e8ff09f551fa33', 'tools/nogo/cli/cli.go': '016543cf5f9ad3d2d75d7a3df22818d109ee6438', 'tools/nogo/defs.bzl': '460bf780ed9204e8a5a565f26b897e74e3fbea31', 'tools/nogo/flags/flags.go': '6572ffe5e6357db9eb6b80d88e05dfba7b98fb4c'}}


UPSTREAM = {
    "before_2513": ("cdbba80c45c02a79c6581e61bc6590c90daccfd0", "12bc04449714fb484bd9d84ba146db0cfb93417778b460e470c998c5c6dd3dcc"),
    "after_2513": ("45c5eebc19c50be34e28edee16acdd47c72dfd25", "336e0800feb0d0fdfb484acc4ca440fd6bf34598cd4db41256374ce0df04fd70"),
}
DEFAULT_SHA = {
    "stock": "68d62b8184488797a92004da081dc5f0f5044ac2ee2427445f4b0242b9c69688",
    "before_2513": "1788b5ab87ef6dd353455f3096d0f4d85ddad61664588e78e5b94514b779f29f",
    "after_2513": "94b84a9d14a40b888f723fe5c9f3beed0cbe6a06a2fee6e6c9a3ed544c2ec4e4",
}
GO_SUM = "h1:44na7Ud+VwyE7LIoJ8JTNQOa549a8543BmzaJHo6Bzo="
DIRECTIVE = "gazelle:go_generate_proto false"


def selection(case):
    if case == "stock":
        return []
    revision, archive_sha = UPSTREAM[case]
    path = AREA / ("bazel-gazelle-" + revision)
    archive = AREA / (revision + ".tar.gz")
    with urllib.request.urlopen("https://codeload.github.com/bazel-contrib/bazel-gazelle/tar.gz/" + revision, timeout=150) as stream:
        archive.write_bytes(stream.read())
    assert digest(archive.read_bytes()) == archive_sha, case
    with tarfile.open(archive) as tar:
        tar.extractall(AREA, filter="data")
    assert (path / "extensions.bzl").is_file(), path
    return ["--override_module=gazelle=" + str(path)]


def measure(case, options):
    row = {"gazelle_archive": UPSTREAM.get(case), "consumer_sha256": digest((CONSUMER / "MODULE.bazel").read_bytes())}
    REPORT[case] = row
    override = selection(case)
    row["selection_flags"] = override
    # Keep the consumer declaration identical, including its lack of a go_deps tag.
    if override:
        COMMON.extend(override)
    selected = run(case + "_repos", CONSUMER, "mod", "show_repo", GAZELLE, CGROUPS)
    if selected is not None:
        (OUT / (case + "-repos.txt")).write_text(selected)
        row["module_version_verified"] = 'version = "v3.0.5"' in selected and GO_SUM in selected
        row["effective_upstream_directive"] = DIRECTIVE in selected
    location = run(case + "_location", CONSUMER, "query", "--output=location", GAZELLE + "//:extensions.bzl")
    if location:
        match = re.search(r"^(.*?):\d+:\d+: ", location, re.M)
        if match:
            rule = (CONSUMER / match[1]).parent / "internal/bzlmod/default_gazelle_overrides.bzl"
            row["defaults_sha256"] = digest(rule.read_bytes())
    for version in (1, 2):
        pkg = f"//cgroup{version}/stats"
        raw = run(case + "_stats" + str(version), CONSUMER, "query", "--output=xml", CGROUPS + pkg + ":all")
        if raw:
            (OUT / f"{case}-cgroup{version}-rules.xml").write_text(raw)
            row[pkg] = [{"kind": rule.get("class"), "name": rule.get("name"),
                         "srcs": [node.get("value").split(":")[-1] for node in rule.findall("list[@name='srcs']/label")]}
                        for rule in ET.fromstring(raw).findall("rule")]
    built = run(case + "_libraries", CONSUMER, "build", *options, CGROUPS + "//cgroup2:cgroup2")
    stderr = (AREA / (case + "_libraries.stderr")).read_text(errors="replace")
    row["missing_symbols"] = [line.strip() for line in stderr.splitlines() if re.match(r"^external/[^ ]+:[0-9]+:[0-9]+: undefined: ",line)][:30]
    row["library_built"] = built is not None
    persist()
    if built is not None and case == "after_2513":
        row["cgroup1_library_built"] = run(case + "_cgroup1", CONSUMER, "build", *options, CGROUPS + "//cgroup1:cgroup1") is not None
        release(case + "_release", options)
    if override:
        COMMON.remove(override[0])


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

    patch = AREA / "pr16098.patch"
    assert digest(patch.read_bytes()) == INTEGRATION["patch_sha256"]
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=SOURCE, check=True)
    subprocess.run(["git", "apply", str(patch)], cwd=SOURCE, check=True)
    for path, oid in INTEGRATION["blobs"].items():
        assert subprocess.check_output(["git", "hash-object", path], cwd=SOURCE, text=True).strip() == oid, path
    REPORT["integrated_sources"] = INTEGRATION
    REPORT["prior_artifact"] = 13959050709
    REPORT["release_flags"] = options
    assert "go_deps" not in minimal
    for case in DEFAULT_SHA:
        measure(case, options)
    for case, expected in DEFAULT_SHA.items():
        row = REPORT[case]
        fixed = case == "after_2513"
        assert row.get("defaults_sha256") == expected, (case, row)
        assert row.get("module_version_verified"), (case, row)
        assert row.get("effective_upstream_directive") == fixed, (case, row)
        assert row["library_built"] == fixed, (case, row)
        if not fixed:
            assert any("undefined: stats.Metrics" in s for s in row["missing_symbols"]), (case, row)
        for version in (1, 2):
            rules = row[f"//cgroup{version}/stats"]
            assert any(r["kind"] == "proto_library" and "metrics.proto" in r["srcs"] for r in rules), rules
            assert any(r["kind"] == "go_library" and "doc.go" in r["srcs"] and
                       ("metrics.pb.go" in r["srcs"]) == fixed for r in rules), rules
    assert REPORT["after_2513"]["cgroup1_library_built"]
    assert len(REPORT["after_2513_release"]["files"]) == 9
    persist()


if __name__ == "__main__":
    main()
