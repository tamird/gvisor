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



INTEGRATION = {'base': 'a066005a68e743f3c8bc0d974a2c6c87f414ba18', 'pr': '66eb2774175258362fb3afaa11d75dec66c3684a', 'integrated_tree': 'a8f2cacb450dff6c03c6e5b432210c7a8c6d547c', 'patch_sha256': '200f2befd976ffb04623281d62f510340ce50fe3354b7c1e85d0c3dfd4a1a88a', 'blobs': {'nogo.yaml': '5adebdb80aa6b2134d1bcd9acd4c348921ee3769', 'tools/bazeldefs/go.bzl': '8fbe4f67166d47d5e748236318093fe62e9e9f35', 'tools/nogo/README.md': 'd49294d7975e6ca1c6aaf0e468e56199fb483752', 'tools/nogo/check/BUILD': 'b9556912b9a176226209719a4b6bed0b54c6ba7b', 'tools/nogo/check/check.go': '1457d40647344e71e51129d5f81f0c64894c07e0', 'tools/nogo/check/check_test.go': '919abec20d99bb7eda692f67d7e8ff09f551fa33', 'tools/nogo/cli/cli.go': '016543cf5f9ad3d2d75d7a3df22818d109ee6438', 'tools/nogo/defs.bzl': '460bf780ed9204e8a5a565f26b897e74e3fbea31', 'tools/nogo/flags/flags.go': '6572ffe5e6357db9eb6b80d88e05dfba7b98fb4c'}}

NOGO = "@gvisor//pkg/prometheus:prometheus_nogo"
METRIC = "gvisor.dev/gvisor/pkg/metric/metric_go_proto"
PROMETHEUS = "gvisor.dev/gvisor/pkg/prometheus"


def action_records(case, options):
    # Query the producing actions and the consumer's real import, for each
    # transitioned architecture selected by the validation target.
    narrowed = 'filter("//(pkg/prometheus:prometheus|pkg/metric:metric_go_proto)$", deps(' + NOGO + '))'
    for attempt, scope in (("owners", narrowed), ("closure", "deps(" + NOGO + ")")):
        expr = 'mnemonic("GoStaticAnalysis|GoCompilePkg", ' + scope + ')'
        output = run(case + "_aquery_" + attempt, CONSUMER, "aquery", *options,
                     "--include_aspects", "--include_commandline", "--include_param_files",
                     "--noinclude_artifacts", "--output=jsonproto", expr)
        if output is None:
            continue
        parsed = json.loads(output)
        actions = []
        owners = {str(t["id"]): t["label"] for t in parsed.get("targets", [])}
        for act in parsed.get("actions", []):
            owner = owners.get(str(act.get("targetId")), "")
            if not owner.endswith(("//pkg/prometheus:prometheus", "//pkg/metric:metric_go_proto")):
                continue
            args = list(act.get("arguments", []))
            for pf in act.get("paramFiles", []):
                args.extend(pf.get("arguments", []))
            selected = []
            for i, arg in enumerate(args):
                if arg.startswith(("-GOOS=", "-GOARCH=", "-package=", "-go=", "-export=")):
                    selected.append(arg)
                elif METRIC in arg or ("metric_go_proto" in arg and
                                        arg.endswith((".go", ".a", ".x", ".export"))):
                    selected.append(arg)
                elif arg in ("-goos", "-goarch") and i + 1 < len(args):
                    selected.extend((arg, args[i + 1]))
            actions.append({"owner": owner, "mnemonic": act["mnemonic"],
                            "configuration": act.get("configurationId"), "arguments": selected})
        REPORT[case + "_actions"] = actions
        persist()
        if any(any(s.startswith(("-import=" + METRIC + "=", "-compiler-import=" + METRIC + "="))
                   for s in rec["arguments"]) for rec in actions):
            return True
    return False


def qualifier(case, caller, options):
    outer(caller)
    (OUT / (case + ".MODULE.bazel")).write_text(caller)
    location = run(case + "_rules_go", CONSUMER, "query", "--output=location", "@@rules_go+//proto:go_proto")
    assert location
    match = re.search(r"^(.*?):\d+:\d+: ", location, re.M)
    proto = (CONSUMER / Path(match[1]).with_name("def.bzl")).read_text()
    REPORT[case + "_proto_DefaultInfo"] = [
        line.strip() for line in proto.splitlines() if "files = depset([archive.data." in line
    ]
    assert REPORT[case + "_proto_DefaultInfo"]
    mappings = action_records(case, options)
    built = run(case + "_nogo", CONSUMER, "build", *options, "--output_groups=nogo_findings", NOGO)
    REPORT[case + "_verified_mappings"] = mappings
    persist()
    return built is not None

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
    caller = minimal + ('bazel_dep(name="gazelle",version="0.51.3",repo_name="bazel_gazelle")\n'
                        'go_deps = use_extension("@bazel_gazelle//:extensions.bzl", "go_deps")\n'
                        'go_deps.gazelle_override(directives=["gazelle:proto disable"],'
                        'path="github.com/containerd/cgroups/v3")\n')
    patch = (SOURCE / "tools/rules_go_export.patch").read_bytes()
    (CONSUMER / "rules_go_export.patch").write_bytes(patch)
    (CONSUMER / "BUILD.bazel").write_text('exports_files(["rules_go_export.patch"])\n')
    patched = caller + ('single_version_override(module_name="rules_go",version="0.65.0",'
                        'patch_strip=1,patches=["//:rules_go_export.patch"])\n')
    REPORT["rules_go_export_patch_sha256"] = digest(patch)
    REPORT["prior_artifact"] = 13955967390
    REPORT["release_flags"] = options
    baseline = qualifier("stock_rules", caller, options)
    exporter = qualifier("only_export_patch", patched, options)
    integrated = AREA / "pr16098.patch"
    assert digest(integrated.read_bytes()) == INTEGRATION["patch_sha256"]
    subprocess.run(["git", "apply", "--check", str(integrated)], cwd=SOURCE, check=True)
    subprocess.run(["git", "apply", str(integrated)], cwd=SOURCE, check=True)
    for path, oid in INTEGRATION["blobs"].items():
        actual = subprocess.check_output(["git", "hash-object", path], cwd=SOURCE, text=True).strip()
        assert actual == oid, path
    REPORT["integrated_sources"] = INTEGRATION
    reviewed = qualifier("pr16098_stock_rules", caller, options)
    if reviewed:
        selected = "pr16098_stock_rules"
    elif exporter:
        subprocess.run(["git", "apply", "-R", str(integrated)], cwd=SOURCE, check=True)
        outer(patched)
        selected = "only_export_patch"
    else:
        selected = None
    REPORT["release_arm"] = selected
    persist()
    if selected:
        output = release(selected + "_release", options)
        if not output:
            failure = REPORT["results"][selected + "_release_build"]["diagnostics"]
            assert any(line.startswith("ERROR:") for line in failure), "Missing new release boundary"
    assert all(REPORT[a + "_verified_mappings"] for a in
               ["stock_rules", "only_export_patch", "pr16098_stock_rules"]), "Missing actual Nogo import evidence"
    if not baseline:
        assert any("not a package file" in line for line in
                   REPORT["results"]["stock_rules_nogo"]["diagnostics"]), "Different stock control failure"
    persist()


if __name__ == "__main__":
    main()
