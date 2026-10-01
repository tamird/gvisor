"""Meta and miscellaneous rules."""

load("@bazel_skylib//:bzl_library.bzl", _bzl_library = "bzl_library")
load("@bazel_skylib//rules:build_test.bzl", _build_test = "build_test")
load("@bazel_skylib//rules:common_settings.bzl", _BuildSettingInfo = "BuildSettingInfo", _bool_flag = "bool_flag")
load("@bazel_skylib//rules:native_binary.bzl", _native_test = "native_test")
load("@bazel_skylib//rules/directory:providers.bzl", "DirectoryInfo")
load("@com_google_protobuf//bazel:proto_library.bzl", _proto_library = "proto_library")
load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")
load("@rules_python//python:py_test.bzl", _py_test = "py_test")
load("@rules_shell//shell:sh_test.bzl", _sh_test = "sh_test")
load("//tools/bazeldefs:cgroup_test.bzl", "cgroup_v1_tags", "cgroup_v1_variant", "with_cgroup_v1")
load("//tools/bazeldefs:test_architectures.bzl", "test_architecture_tags", "test_architecture_variants", "with_test_architecture")

bzl_library = _bzl_library
bool_flag = _bool_flag
BuildSettingInfo = _BuildSettingInfo

build_amd64_test, _build_amd64_transition = with_test_architecture(_build_test, "amd64").build()
build_arm64_test, _build_arm64_transition = with_test_architecture(_build_test, "arm64").build()
build_test_cgroup_v1_test, _build_test_cgroup_v1_transition = with_cgroup_v1(_build_test)

def build_test(name, architectures = ["amd64", "arm64"], **kwargs):
    """Declares the original test and manual architecture/cgroup variants."""
    kwargs["tags"] = cgroup_v1_tags(test_architecture_tags(architectures, kwargs.get("tags", [])))
    _build_test(name = name, **kwargs)
    cgroup_v1_variant(name, build_test_cgroup_v1_test, kwargs)
    test_architecture_variants(
        name,
        architectures,
        {"amd64": build_amd64_test, "arm64": build_arm64_test},
        kwargs,
    )

native_amd64_test, _native_amd64_transition = with_test_architecture(_native_test, "amd64").build()
native_arm64_test, _native_arm64_transition = with_test_architecture(_native_test, "arm64").build()
native_test_cgroup_v1_test, _native_test_cgroup_v1_transition = with_cgroup_v1(_native_test)

def native_test(name, architectures = ["amd64", "arm64"], **kwargs):
    """Declares the original test and manual architecture/cgroup variants."""
    kwargs["tags"] = cgroup_v1_tags(test_architecture_tags(architectures, kwargs.get("tags", [])))
    _native_test(name = name, **kwargs)
    cgroup_v1_variant(name, native_test_cgroup_v1_test, kwargs)
    test_architecture_variants(
        name,
        architectures,
        {"amd64": native_amd64_test, "arm64": native_arm64_test},
        kwargs,
    )

py_amd64_test, _py_amd64_transition = with_test_architecture(_py_test, "amd64").build()
py_arm64_test, _py_arm64_transition = with_test_architecture(_py_test, "arm64").build()
py_test_cgroup_v1_test, _py_test_cgroup_v1_transition = with_cgroup_v1(_py_test)

def py_test(name, architectures = ["amd64", "arm64"], **kwargs):
    """Declares the original test and manual architecture/cgroup variants."""
    kwargs["tags"] = cgroup_v1_tags(test_architecture_tags(architectures, kwargs.get("tags", [])))
    _py_test(name = name, **kwargs)
    cgroup_v1_variant(name, py_test_cgroup_v1_test, kwargs)
    test_architecture_variants(
        name,
        architectures,
        {"amd64": py_amd64_test, "arm64": py_arm64_test},
        kwargs,
    )

sh_amd64_test, _sh_amd64_transition = with_test_architecture(_sh_test, "amd64").build()
sh_arm64_test, _sh_arm64_transition = with_test_architecture(_sh_test, "arm64").build()
sh_test_cgroup_v1_test, _sh_test_cgroup_v1_transition = with_cgroup_v1(_sh_test)

def sh_test(name, architectures = ["amd64", "arm64"], **kwargs):
    """Declares the original test and manual architecture/cgroup variants."""
    kwargs["tags"] = cgroup_v1_tags(test_architecture_tags(architectures, kwargs.get("tags", [])))
    _sh_test(name = name, **kwargs)
    cgroup_v1_variant(name, sh_test_cgroup_v1_test, kwargs)
    test_architecture_variants(
        name,
        architectures,
        {"amd64": sh_amd64_test, "arm64": sh_arm64_test},
        kwargs,
    )

# Public language and Kubernetes qualification gives each test 1800 seconds.
# Use an output-only transition: with_cfg also reads test_timeout, whose native
# Map<TestTimeout, Duration> value is not readable by Starlark in Bazel 8.5.
# https://github.com/bazelbuild/bazel/blob/d84820503/src/main/java/com/google/devtools/build/lib/analysis/config/StarlarkDefinedConfigTransition.java#L416-L471
runtime_test_timeout = transition(
    implementation = lambda _settings, _attr: {"//command_line_option:test_timeout": "1800"},
    inputs = [],
    outputs = ["//command_line_option:test_timeout"],
)

runtime_native_test = rule(
    doc = "A native test with the public runtime qualification timeout.",
    implementation = lambda ctx: ctx.super(),
    parent = _native_test,
    cfg = runtime_test_timeout,
)

more_shards = 4
most_shards = 8
version = "//tools/bazeldefs:version"

def short_path(path):
    return path

def proto_library(name, has_services = None, **kwargs):  # buildifier: disable=unused-variable
    _proto_library(
        name = name,
        **kwargs
    )

def select_arch(amd64 = None, arm64 = None, riscv64 = None, default = None, **kwargs):
    """Select an option against standard architectures.

    Args:
      amd64: the option if the architecture is amd64.
      arm64: the option if the architecture is arm64.
      riscv64: the option if the architecture is riscv64.
      default: the option if no matching architecture is provided.
      **kwargs: extra select arguments.

    Returns:
      An appropriate select."""
    values = dict()
    if amd64 != None:
        values[Label("//tools/bazeldefs:amd64")] = amd64
    if arm64 != None:
        values[Label("//tools/bazeldefs:arm64")] = arm64
    if riscv64 != None:
        values[Label("//tools/bazeldefs:riscv64")] = riscv64
    if default != None:
        values["//conditions:default"] = default
    return select(values, **kwargs)

def select_system(linux = ["__linux__"], darwin = [], **_kwargs):
    return select({
        "@bazel_tools//src/conditions:darwin": darwin,
        "//conditions:default": linux,
    })

arch_config = [
    "@io_bazel_rules_go//go/config:race",
    "//command_line_option:cpu",
    "//command_line_option:platforms",
]

def arm64_config(_settings, _attr):
    return {
        # Disable the inherited race setting for cross-architecture generation.
        # Targets with an explicit race attribute still use instrumentation.
        "@io_bazel_rules_go//go/config:race": False,
        "//command_line_option:cpu": "aarch64",
        # Permit targets that explicitly enable race instrumentation to use cgo.
        # Ordinary targets still inherit the pure build setting from .bazelrc.
        "//command_line_option:platforms": "@io_bazel_rules_go//go/toolchain:linux_arm64_cgo",
    }

def amd64_config(_settings, _attr):
    return {
        # See above.
        "@io_bazel_rules_go//go/config:race": False,
        "//command_line_option:cpu": "k8",
        # See above.
        "//command_line_option:platforms": "@io_bazel_rules_go//go/toolchain:linux_amd64_cgo",
    }

transition_allowlist = "@bazel_tools//tools/allowlists/function_transition_allowlist"

def default_installer():
    return None

def default_net_util():
    return []  # Nothing needed.

def coreutil():
    return []  # Nothing needed.

def _bpf_program_impl(ctx):
    resource_dir = ctx.attr._clang_resource_dir[DirectoryInfo]
    kernel_headers = ctx.attr._kernel_headers[DirectoryInfo]
    libbpf = ctx.attr._libbpf[CcInfo].compilation_context

    args = ctx.actions.args()
    args.add_all(["-O2", "-Wall", "-Werror", "--target=bpfel"])

    # Keep the oldest BPF ISA instead of inheriting Clang's default (now v3):
    # https://github.com/llvm/llvm-project/blob/llvmorg-23.1.0/llvm/lib/Target/BPF/BPFSubtarget.cpp#L75
    args.add("-mcpu=v1")
    args.add("-nostdinc")
    args.add("-resource-dir", resource_dir.path)
    args.add("-isystem", resource_dir.path + "/include")
    args.add("-isystem", kernel_headers.path)
    args.add_all(libbpf.includes, before_each = "-isystem")
    args.add_all(libbpf.quote_includes, before_each = "-iquote")
    args.add_all(libbpf.system_includes, before_each = "-isystem")
    args.add("-c", ctx.file.src)
    args.add("-o", ctx.outputs.bpf_object)
    ctx.actions.run(
        executable = ctx.executable._clang,
        arguments = [args],
        inputs = depset(
            [ctx.file.src],
            transitive = [
                resource_dir.transitive_files,
                kernel_headers.transitive_files,
                libbpf.headers,
            ],
        ),
        outputs = [ctx.outputs.bpf_object],
        mnemonic = "BPFCompile",
        progress_message = "Compiling BPF program %{label}",
    )
    return [DefaultInfo(files = depset([ctx.outputs.bpf_object]))]

bpf_program = rule(
    implementation = _bpf_program_impl,
    doc = "Compiles a BPF program with declared compiler and header inputs.",
    attrs = {
        "src": attr.label(allow_single_file = [".c"], mandatory = True),
        "bpf_object": attr.output(mandatory = True),
        "_clang": attr.label(
            default = "//tools/bazeldefs:bpf_clang",
            executable = True,
            allow_single_file = True,
            cfg = "exec",
        ),
        "_clang_resource_dir": attr.label(
            default = "//tools/bazeldefs:clang_resource_dir",
            providers = [DirectoryInfo],
            cfg = "exec",
        ),
        "_kernel_headers": attr.label(
            default = "@kernel_headers//:kernel_headers_directory",
            providers = [DirectoryInfo],
        ),
        "_libbpf": attr.label(default = "@libbpf//:bpf", providers = [CcInfo]),
    },
)
