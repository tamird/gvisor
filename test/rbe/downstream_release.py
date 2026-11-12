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
REPORT = {"upstream": STOCK, "compiler": COMPILER, "results": {}}


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
    path = Path(match[1]).with_name("rand.go")
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
    expr = 'inputs("internal/detrand/rand[.]go$", mnemonic("GoCompilePkg", deps(' + label + ')))'
    result = run(name, cwd, "aquery", *flags(prefix), "--output=text", "--include_commandline=false", expr)
    if result is None:
        return False
    rows = [line.strip() for line in result.splitlines() if "Target:" in line or "Mnemonic:" in line or "Configuration:" in line]
    matches = sorted(set(re.findall(r"[^\s,\[\]]*internal/detrand/rand\.go", result)))
    assert matches and any("GoCompilePkg" in r for r in rows), (name, rows, matches)
    REPORT["results"][name]["actions"] = rows
    REPORT["results"][name]["declared_inputs"] = matches
    persist()
    return True


def main():
    CONSUMER.mkdir()
    (CONSUMER / "BUILD.bazel").write_text('alias(name="release",actual="@gvisor//:release")\n')
    prefix = ('module(name="downstream_release")\n'
              'bazel_dep(name="gvisor",version="0.0.1")\n'
              f'local_path_override(module_name="gvisor",path={json.dumps(str(SOURCE))})\n'
              'bazel_dep(name="gazelle",version="0.51.3",repo_name="bazel_gazelle")\n'
              'bazel_dep(name="rules_go",version="0.65.0",repo_name="io_bazel_rules_go")\n'
              'bazel_dep(name="protobuf",version="35.1",repo_name="com_google_protobuf")\n'
              'go_deps=use_extension("@bazel_gazelle//:extensions.bzl","go_deps")\n'
              'use_repo(go_deps,"org_golang_google_protobuf")\n')
    module = SOURCE / "MODULE.bazel"
    for arm, revision in (("stock", STOCK), ("compiler", COMPILER)):
        subprocess.run(["git", "checkout", "--quiet", "--detach", revision], check=True)
        outer(prefix)
        protobuf(arm + "_external_proto", CONSUMER)
        release_analysis(arm + "_external_release", CONSUMER, "@gvisor")
    original = module.read_text()
    root = protobuf("compiler_root_proto", SOURCE)
    if root is not None:
        assert root["disabled_seed"] and not root["reads_executable"]
    root_paths = release_analysis("compiler_root_release", SOURCE, "")
    if root_paths:
        action("compiler_root_protobuf_compile", SOURCE, "")

    # Replay #16804 and the independently researched Go tag split. Each
    # amendment affects only the extension usage in the supplier MODULE.
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
    plain = protobuf("ownership_external_defaults_proto", CONSUMER)
    if plain is not None:
        assert plain["reads_executable"] and not plain["disabled_seed"]
    # A dependent module's single_version_override directives are ignored
    # by Bazel. An adopting root reuses all three existing exact patch sets.
    overrides = re.findall(r"(?ms)^single_version_override\(\n.*?^\)\n", original)
    assert len(overrides) == 3
    adopted = (prefix + 'bazel_dep(name="llvm",version="0.8.24")\n' +
               "\n".join(overrides).replace('"//tools:', '"@gvisor//tools:') +
               tags.replace('"//tools:', '"@gvisor//tools:') +
               '\nllvm_headers=use_extension("@llvm//kernel/extension:kernel_headers.bzl","kernel_headers")\n'
               'llvm_headers.configure(default_version="6.18")\n')
    outer(adopted)
    (OUT / "adopting.MODULE.bazel").write_text(adopted)
    adopted_source = protobuf("ownership_external_adopted_proto", CONSUMER)
    if adopted_source is not None and root is not None:
        assert adopted_source == root
    paths = release_analysis("ownership_external_adopted_release", CONSUMER, "@gvisor")
    if not paths or adopted_source is None or root is None:
        REPORT["build_disposition"] = "Stopped before execution. Inspect the consumer analysis and protobuf comparison."
        persist()
        return
    if root_paths:
        assert {p.split("/release/")[-1] for p in paths} == {p.split("/release/")[-1] for p in root_paths}
    assert action("ownership_external_protobuf_compile", CONSUMER, "@gvisor")
    build = run("ownership_external_build", CONSUMER, "build", *flags("@gvisor"), "@gvisor//:release")
    if build is None:
        return
    execution_root = subprocess.check_output([str(AREA / "bazel"), "--nosystem_rc", "--nohome_rc",
        "--noworkspace_rc", f"--bazelrc={AREA / 'client.bazelrc'}", "info", "execution_root"], cwd=CONSUMER, text=True).strip()
    records = []
    for output in paths:
        path = Path(execution_root) / output
        with path.open("rb") as f:
            head = f.read(20)
            assert head[:4] == b"\x7fELF" and head[5] == 1 and int.from_bytes(head[18:20], "little") == 183
            f.seek(0)
            checksum = hashlib.file_digest(f, "sha256").hexdigest()
        records.append({"member": output.split("/release/")[-1], "format": "ELF AArch64", "sha256": checksum})
    REPORT["files"] = records
    persist()


if __name__ == "__main__":
    main()
