# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Toolchain and execution requirements for the optional network plugin."""

load("@io_bazel_rules_go//go:def.bzl", "go_test")
load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain", "use_cc_toolchain")

def _plugin_toolchain_impl(ctx):
    toolchain = find_cc_toolchain(ctx)
    if toolchain.compiler not in ["gcc", "clang"]:
        fail("The plugin requires a GCC or Clang toolchain, got {}".format(toolchain.compiler))

    # rules_foreign_cc supplies CC, AR and LD, but not objdump. The Makefile
    # resolves a relative path against foreign_cc's execution root before
    # recursive builds change directories.
    return [
        platform_common.TemplateVariableInfo({"PLUGIN_OBJDUMP": toolchain.objdump_executable}),
        DefaultInfo(
            files = toolchain.all_files,
            runfiles = ctx.runfiles(transitive_files = toolchain.all_files),
        ),
    ]

plugin_toolchain = rule(
    implementation = _plugin_toolchain_impl,
    fragments = ["cpp"],
    toolchains = use_cc_toolchain(),
)

# The backtrace test is statically linked. Its runner needs the target OS and
# CPU, but does not need the libc used to link the test binary.
plugin_backtrace_test = rule(
    implementation = lambda ctx: ctx.super(),
    parent = go_test,
    exec_groups = {
        "test": exec_group(exec_compatible_with = [
            "@platforms//cpu:x86_64",
            "@platforms//os:linux",
        ]),
    },
)
