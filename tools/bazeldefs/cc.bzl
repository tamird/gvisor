"""C++ rules."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@com_github_grpc_grpc//bazel:cc_grpc_library.bzl", _cc_grpc_library = "cc_grpc_library")
load("@com_google_protobuf//bazel:cc_proto_library.bzl", _cc_proto_library = "cc_proto_library")
load("@rules_cc//cc:action_names.bzl", "ACTION_NAMES")
load("@rules_cc//cc:defs.bzl", _cc_binary = "cc_binary", _cc_library = "cc_library", _cc_test = "cc_test")
load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain", "use_cc_toolchain")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load(":defs.bzl", "transition_allowlist")

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

cc_binary = _cc_binary
cc_test = _cc_test
cc_proto_library = _cc_proto_library
cc_linker = "@llvm//tools:ld.lld"
cc_objcopy = "@llvm//tools:llvm-objcopy"
cc_objdump = "@llvm//tools:llvm-objdump"
cc_nm = "@llvm//tools:llvm-nm"
gtest = "@com_google_googletest//:gtest"
gbenchmark = "@com_google_benchmark//:benchmark"
gbenchmark_internal = "@com_google_benchmark//:benchmark"
grpcpp = "@com_github_grpc_grpc//:grpc++"

def _cc_flags_supplier_impl(ctx):
    toolchain = find_cc_toolchain(ctx)
    features = cc_common.configure_features(
        ctx = ctx,
        cc_toolchain = toolchain,
        requested_features = ctx.features,
        # Freestanding consumers do not link a coverage runtime. Keep native
        # instrumentation disabled when the surrounding Go build uses coverage.
        unsupported_features = ctx.disabled_features + ["coverage"],
    )
    cxx = ctx.attr.language == "c++"
    action = ACTION_NAMES.cpp_compile if cxx else ACTION_NAMES.c_compile

    # The consumers link freestanding binaries. Compilation settings supply
    # target and header paths without adding the toolchain's runtime libraries.
    compile_variables = cc_common.create_compile_variables(
        feature_configuration = features,
        cc_toolchain = toolchain,
        user_compile_flags = ctx.fragments.cpp.copts + (ctx.fragments.cpp.cxxopts if cxx else ctx.fragments.cpp.conlyopts),
    )
    flags = cc_common.get_memory_inefficient_command_line(
        feature_configuration = features,
        action_name = action,
        variables = compile_variables,
    )
    variables = platform_common.TemplateVariableInfo({
        "CC": shell.quote(cc_common.get_tool_for_action(
            feature_configuration = features,
            action_name = action,
        )),
        "CC_FLAGS": " ".join([shell.quote(flag) for flag in flags]),
    })
    return [variables, DefaultInfo(files = toolchain.all_files)]

cc_flags_supplier = rule(
    implementation = _cc_flags_supplier_impl,
    attrs = {"language": attr.string(default = "c", values = ["c", "c++"])},
    fragments = ["cpp"],
    toolchains = use_cc_toolchain(),
)

def cc_grpc_library(name, **kwargs):
    _cc_grpc_library(name = name, grpc_only = True, **kwargs)

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

freestanding_executable = rule(
    implementation = _sidecar_executable_impl,
    doc = "Exposes a freestanding C executable without a hosted allocator or sanitizer.",
    executable = True,
    attrs = {
        "binary": attr.label(allow_single_file = True, cfg = _sidecar_transition, mandatory = True),
        "out": attr.output(mandatory = True),
        "_allowlist_function_transition": attr.label(default = transition_allowlist),
    },
)

def select_gtest():
    return [gtest]  # No select is needed.
