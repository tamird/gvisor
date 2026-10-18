# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Build Linux kernels and selftests from declared source and tools."""

load("@bazel_skylib//rules/directory:providers.bzl", "DirectoryInfo")
load("@rules_foreign_cc//toolchains/native_tools:tool_access.bzl", "access_tool")
load("@tar.bzl//tar:tar.bzl", "tar_lib")

_LLVM_TOOLS = ["clang", "ld.lld", "llvm-ar", "llvm-nm", "llvm-objcopy", "llvm-objdump", "llvm-readelf", "llvm-strip"]

def _build(ctx, output, configuration, mnemonic):
    make = access_tool(Label("@rules_foreign_cc//toolchains:make_toolchain"), ctx)
    if make.target == None or make.env != {"MAKE": make.path}:
        fail("The kernel build requires the declared GNU Make toolchain")
    tar = ctx.toolchains[tar_lib.toolchain_type]
    resource = ctx.attr._resource_dir[DirectoryInfo]
    evidence = ctx.actions.declare_directory(ctx.label.name + ".evidence")
    config = ctx.actions.declare_file(ctx.label.name + ".json")
    llvm = {name: getattr(ctx.attr, "_" + name.replace("-", "_").replace(".", "_")) for name in _LLVM_TOOLS}
    ctx.actions.write(config, json.encode({
        "source": ctx.file._source.path,
        "sysroot": ctx.file._sysroot.path,
        "llvm": {name: target[DefaultInfo].files_to_run.executable.path for name, target in llvm.items()},
        "resource_dir": resource.path,
        "make": make.path,
        "tar": tar.tarinfo.binary.path,
        "tar_env": tar.tarinfo.default_env,
        "output": output.path,
        "evidence": evidence.path,
    } | configuration))
    ctx.actions.run(
        executable = ctx.executable._builder,
        arguments = [config.path],
        inputs = depset([config, ctx.file._source, ctx.file._sysroot], transitive = [
            target[DefaultInfo].files
            for target in llvm.values()
        ] + [resource.transitive_files, make.target.files, tar.default.files]),
        tools = [ctx.attr._builder[DefaultInfo].files_to_run, tar.tarinfo.binary],
        outputs = [output, evidence],
        mnemonic = mnemonic,
        progress_message = "Building %{label}",
    )
    return evidence

def _kernel_impl(ctx):
    archive = ctx.actions.declare_file(ctx.label.name + ".tar")
    evidence = _build(ctx, archive, {
        "architecture": ctx.attr.architecture,
        "release": ctx.attr.release,
    }, "LinuxTestKernel")
    return [
        DefaultInfo(files = depset([archive])),
        OutputGroupInfo(build_evidence = depset([evidence])),
    ]

def _selftest_impl(ctx):
    executable = ctx.actions.declare_file(ctx.label.name)
    evidence = _build(ctx, executable, {
        "architecture": "x86_64",
        "selftest": ctx.attr.selftest,
    }, "LinuxSelftest")
    return [
        DefaultInfo(executable = executable),
        OutputGroupInfo(build_evidence = depset([evidence])),
    ]

_TOOL_ATTRS = {
    "_source": attr.label(default = "@kcov_linux_source//file", allow_single_file = True),
    "_sysroot": attr.label(default = "@kernel_build_deps//:flat", allow_single_file = True, cfg = "exec"),
    "_builder": attr.label(default = ":build", executable = True, cfg = "exec"),
    "_resource_dir": attr.label(default = "@llvm//:builtin_resource_dir", providers = [DirectoryInfo], cfg = "exec"),
} | {
    "_" + name.replace("-", "_").replace(".", "_"): attr.label(
        default = "@llvm//tools:" + name,
        executable = True,
        allow_single_file = True,
        cfg = "exec",
    )
    for name in _LLVM_TOOLS
}

_TOOLCHAINS = [Label("@rules_foreign_cc//toolchains:make_toolchain"), Label(tar_lib.toolchain_type)]

linux_kernel = rule(
    implementation = _kernel_impl,
    attrs = _TOOL_ATTRS | {
        "architecture": attr.string(default = "x86_64", values = ["x86_64", "arm64"]),
        "release": attr.string(mandatory = True),
    },
    toolchains = _TOOLCHAINS,
)

linux_selftest = rule(
    implementation = _selftest_impl,
    attrs = _TOOL_ATTRS | {
        "selftest": attr.string(mandatory = True),
        "_sysroot": attr.label(default = ":selftest_sysroot", allow_single_file = True, cfg = "exec"),
    },
    executable = True,
    toolchains = _TOOLCHAINS,
)
