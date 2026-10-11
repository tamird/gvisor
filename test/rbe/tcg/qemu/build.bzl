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

"""Build the two QEMU diagnostic arms with one declared compiler closure."""

load("@bazel_skylib//rules/directory:providers.bzl", "DirectoryInfo")
load("@rules_foreign_cc//toolchains/native_tools:tool_access.bzl", "access_tool")
load("@tar.bzl//tar:tar.bzl", "tar_lib")

QemuPairInfo = provider(fields = ["before", "after", "evidence"])

def _build_impl(ctx):
    make = access_tool(Label("@rules_foreign_cc//toolchains:make_toolchain"), ctx)
    if make.target == None or make.env != {"MAKE": make.path}:
        fail("The QEMU diagnostic requires the declared GNU Make toolchain")
    tar = ctx.toolchains[tar_lib.toolchain_type]
    resource = ctx.attr._resource_dir[DirectoryInfo]
    before = ctx.actions.declare_file(ctx.label.name + "/before/qemu-system-aarch64")
    after = ctx.actions.declare_file(ctx.label.name + "/after/qemu-system-aarch64")
    evidence = ctx.actions.declare_directory(ctx.label.name + "/evidence")
    config = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(config, json.encode({
        "source_root": ctx.file._version.dirname,
        "sources": [file.path for file in ctx.files._sources],
        "sysroot": ctx.file._sysroot.path,
        "patch": ctx.file.patch.path,
        "cc": ctx.executable._clang.path,
        "cxx": ctx.executable._clangxx.path,
        "linker": ctx.executable._linker.path,
        "ar": ctx.executable._ar.path,
        "nm": ctx.executable._nm.path,
        "ranlib": ctx.executable._ranlib.path,
        "objcopy": ctx.executable._objcopy.path,
        "strip": ctx.executable._strip.path,
        "resource_dir": resource.path,
        "make": make.path,
        "tar": tar.tarinfo.binary.path,
        "tar_env": tar.tarinfo.default_env,
        "before": before.path,
        "after": after.path,
        "evidence": evidence.path,
    }))
    tools = [ctx.attr._builder[DefaultInfo].files_to_run, tar.tarinfo.binary]
    inputs = depset([config, ctx.file._sysroot, ctx.file.patch], transitive = [
        ctx.attr._sources[DefaultInfo].files,
        ctx.attr._clang[DefaultInfo].files,
        ctx.attr._clangxx[DefaultInfo].files,
        ctx.attr._linker[DefaultInfo].files,
        ctx.attr._ar[DefaultInfo].files,
        ctx.attr._nm[DefaultInfo].files,
        ctx.attr._ranlib[DefaultInfo].files,
        ctx.attr._objcopy[DefaultInfo].files,
        ctx.attr._strip[DefaultInfo].files,
        resource.transitive_files,
        make.target.files,
        tar.default.files,
    ])
    ctx.actions.run(
        executable = ctx.executable._builder,
        arguments = [config.path],
        inputs = inputs,
        tools = tools,
        outputs = [before, after, evidence],
        mnemonic = "QemuRcscPairBuild",
        progress_message = "Building QEMU before and after the ARM RCsc barrier",
    )
    return [
        DefaultInfo(files = depset([before, after, evidence])),
        QemuPairInfo(before = before, after = after, evidence = evidence),
    ]

qemu_pair = rule(
    implementation = _build_impl,
    attrs = {
        "patch": attr.label(allow_single_file = True, mandatory = True),
        "_sources": attr.label(default = "@qemu_source//:sources"),
        "_version": attr.label(default = "@qemu_source//:VERSION", allow_single_file = True),
        "_sysroot": attr.label(default = "@qemu_build_deps//:flat", allow_single_file = True, cfg = "exec"),
        "_builder": attr.label(default = ":build", executable = True, cfg = "exec"),
        "_clang": attr.label(default = "@llvm//tools:clang", executable = True, allow_single_file = True, cfg = "exec"),
        "_clangxx": attr.label(default = "@llvm//tools:clang++", executable = True, allow_single_file = True, cfg = "exec"),
        "_linker": attr.label(default = "@llvm//tools:ld.lld", executable = True, allow_single_file = True, cfg = "exec"),
        "_ar": attr.label(default = "@llvm//tools:llvm-ar", executable = True, allow_single_file = True, cfg = "exec"),
        "_nm": attr.label(default = "@llvm//tools:llvm-nm", executable = True, allow_single_file = True, cfg = "exec"),
        "_ranlib": attr.label(default = "@llvm//tools:llvm-ranlib", executable = True, allow_single_file = True, cfg = "exec"),
        "_objcopy": attr.label(default = "@llvm//tools:llvm-objcopy", executable = True, allow_single_file = True, cfg = "exec"),
        "_strip": attr.label(default = "@llvm//tools:llvm-strip", executable = True, allow_single_file = True, cfg = "exec"),
        "_resource_dir": attr.label(default = "@llvm//:builtin_resource_dir", providers = [DirectoryInfo], cfg = "exec"),
    },
    toolchains = [Label("@rules_foreign_cc//toolchains:make_toolchain"), Label(tar_lib.toolchain_type)],
)
