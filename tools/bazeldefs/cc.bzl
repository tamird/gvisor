"""C++ rules."""

load("@com_github_grpc_grpc//bazel:cc_grpc_library.bzl", _cc_grpc_library = "cc_grpc_library")
load("@com_google_protobuf//bazel:cc_proto_library.bzl", _cc_proto_library = "cc_proto_library")
load("@llvm//toolchain/runtimes:cc_runtime_binary.bzl", "cc_runtime_stage0_binary", "cc_runtime_stage1_hosted_binary")
load("@rules_cc//cc:cc_rules_extension_support.bzl", _cc_test_rule = "cc_test")
load("@rules_cc//cc:defs.bzl", _cc_binary = "cc_binary", _cc_library = "cc_library", _cc_test = "cc_test")
load("//tools/bazeldefs:test_architectures.bzl", "test_architecture_variants", "with_test_architecture")
load(":defs.bzl", "transition_allowlist")

cc_amd64_test, _cc_amd64_transition = with_test_architecture(_cc_test_rule, "amd64").build()
cc_arm64_test, _cc_arm64_transition = with_test_architecture(_cc_test_rule, "arm64").build()

def cc_test(name, architectures = [], **kwargs):
    """Declares a C++ test and optional native architecture variants."""
    _cc_test(name = name, **kwargs)
    # The public cc_test macro adds the extra link library. The architecture
    # variants inherit the underlying rule, so supply that dependency here.
    if architectures and not kwargs.get("linkshared"):
        kwargs["deps"] = (kwargs.get("deps") or []) + [Label("@rules_cc//:link_extra_lib")]
    test_architecture_variants(
        name,
        architectures,
        {"amd64": cc_amd64_test, "arm64": cc_arm64_test},
        kwargs,
    )

def cc_library(**kwargs):
    """Wraps _cc_library and deduplicates deps.

    Args:
      **kwargs: arguments passed to _cc_library.
    """
    if "deps" in kwargs and type(kwargs["deps"]) == "list":
        # Dedupe dep entries. Needed due to uninteresting quirks.
        # Don't remove.
        deps = []
        for d in kwargs["deps"]:
            if d not in deps:
                deps.append(d)
        kwargs["deps"] = deps
    _cc_library(**kwargs)

cc_proto_library = _cc_proto_library
cc_toolchain = "@bazel_tools//tools/cpp:current_cc_toolchain"
cc_objcopy = "@llvm//tools:llvm-objcopy"
cc_objdump = "@llvm//tools:llvm-objdump"
cc_readelf = "@llvm//tools:llvm-readelf"
linux_headers = Label("@kernel_headers//:kernel_headers")
gtest = "@com_google_googletest//:gtest"
gbenchmark = "@com_google_benchmark//:benchmark"
gbenchmark_internal = "@com_google_benchmark//:benchmark"
grpcpp = "@com_github_grpc_grpc//:grpc++"

_CC_TOOLCHAIN_TYPE = "@bazel_tools//tools/cpp:toolchain_type"
_LLVM_REPO = Label("@llvm//toolchain:all").repo_name

def _using_llvm_aspect_impl(_target, ctx):
    if ctx.rule.toolchains and _CC_TOOLCHAIN_TYPE in ctx.rule.toolchains:
        selected = ctx.rule.toolchains[_CC_TOOLCHAIN_TYPE].label
        return [config_common.FeatureFlagInfo(value = str(selected.repo_name == _LLVM_REPO))]
    return []

_using_llvm_aspect = aspect(
    implementation = _using_llvm_aspect_impl,
    toolchains_aspects = [_CC_TOOLCHAIN_TYPE],
)

def _using_llvm_impl(ctx):
    return [ctx.attr._cc_toolchain[config_common.FeatureFlagInfo]]

using_llvm = rule(
    implementation = _using_llvm_impl,
    doc = "Whether the selected C toolchain is provided by hermetic-llvm.",
    attrs = {"_cc_toolchain": attr.label(default = cc_toolchain, aspects = [_using_llvm_aspect])},
)

def _cc_flags_supplier_impl(_ctx):
    variables = platform_common.TemplateVariableInfo({
        "CC_FLAGS": "",
    })
    return [variables]

cc_flags_supplier = rule(
    implementation = _cc_flags_supplier_impl,
)

def cc_grpc_library(name, **kwargs):
    _cc_grpc_library(name = name, grpc_only = True, **kwargs)

def cc_binary(name, static = False, tcmalloc = False, **kwargs):
    """Run cc_binary.

    Args:
        name: name of the target.
        static: make a static binary if True
        tcmalloc: use TCMalloc if True (not implemented)
        **kwargs: the rest of the args.
    """
    if static:
        # How to statically link a c++ program that uses threads, like for gRPC:
        # https://gcc.gnu.org/legacy-ml/gcc-help/2010-05/msg00029.html
        if "linkopts" not in kwargs:
            kwargs["linkopts"] = []
        kwargs["linkopts"] += [
            "-static",
            "-lstdc++",
            "-Wl,--whole-archive",
            "-lpthread",
            "-Wl,--no-whole-archive",
        ]
    if tcmalloc:
        # buildifier: disable=print
        print("Warning: tcmalloc can't be enabled")

    _cc_binary(
        name = name,
        **kwargs
    )

def _sidecar_options(settings, _attr):
    # Unset the command-line allocator override so the binary's own malloc
    # attribute takes effect. Disabling instrumentation does not remove a
    # runtime that the user explicitly added to the link.
    sanitizers = ("asan", "hwasan", "lsan", "msan", "tsan", "ubsan")
    runtime_libs = ["-l" + name for name in sanitizers]
    return {
        "//command_line_option:custom_malloc": None,
        "//command_line_option:features": [
            feature
            for feature in settings["//command_line_option:features"]
            if feature not in sanitizers
        ],
        "//command_line_option:linkopt": [
            option
            for option in settings["//command_line_option:linkopt"]
            if not option.startswith("-fsanitize=") and option not in runtime_libs
        ] + ["-fno-sanitize=all"],
    }

_sidecar_transition = transition(
    implementation = _sidecar_options,
    inputs = ["//command_line_option:features", "//command_line_option:linkopt"],
    outputs = ["//command_line_option:custom_malloc", "//command_line_option:features", "//command_line_option:linkopt"],
)

def _sidecar_executable_impl(ctx):
    [binary] = ctx.files.binary
    ctx.actions.symlink(output = ctx.outputs.out, target_file = binary, is_executable = True)
    return [DefaultInfo(executable = ctx.outputs.out)]

_freestanding_executable = rule(
    implementation = _sidecar_executable_impl,
    doc = "Exposes a freestanding C executable without a hosted allocator or sanitizer.",
    executable = True,
    attrs = {
        "binary": attr.label(allow_single_file = True, cfg = _sidecar_transition, mandatory = True),
        "out": attr.output(mandatory = True),
        "_allowlist_function_transition": attr.label(default = transition_allowlist),
    },
)

def freestanding_executable(name, out, libc_headers = False, visibility = None, **kwargs):
    """Builds and exposes an executable without hosted runtime dependencies.

    Args:
      name: Public target name.
      out: Executable filename.
      libc_headers: Whether the source includes target libc headers.
      visibility: Visibility of the public executable.
      **kwargs: Arguments for the private C binary, including explicit headers.
    """
    binary = cc_runtime_stage1_hosted_binary if libc_headers else cc_runtime_stage0_binary
    binary(
        name = "_linked/" + out,
        tags = ["manual"],
        visibility = ["//visibility:private"],
        **kwargs
    )
    _freestanding_executable(
        name = name,
        binary = ":_linked/" + out,
        out = out,
        visibility = visibility,
    )

def select_gtest():
    return [gtest]  # No select is needed.
