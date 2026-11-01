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

"""Additional selected C toolchain variables needed by the plugin's Make build."""

load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain", "use_cc_toolchain")

def _plugin_toolchain_impl(ctx):
    toolchain = find_cc_toolchain(ctx)
    if toolchain.compiler not in ["gcc", "clang"]:
        fail("The plugin requires a GCC or Clang toolchain, got {}".format(toolchain.compiler))

    # Action-based toolchains may not populate the legacy ld/objdump paths.
    # Callers can provide the supplier's executable labels in that case.
    tools = [tool for tool in [ctx.file.linker, ctx.file.objdump] if tool]
    ld = ctx.file.linker.path if ctx.file.linker else toolchain.ld_executable
    objdump = ctx.file.objdump.path if ctx.file.objdump else toolchain.objdump_executable
    files = depset(tools, transitive = [toolchain.all_files])

    # The Makefile resolves relative paths against foreign_cc's execution root
    # before recursive builds change directories.
    return [
        platform_common.TemplateVariableInfo({
            "PLUGIN_LD": ld,
            "PLUGIN_OBJDUMP": objdump,
        }),
        DefaultInfo(
            files = files,
            runfiles = ctx.runfiles(transitive_files = files),
        ),
    ]

plugin_toolchain = rule(
    implementation = _plugin_toolchain_impl,
    attrs = {
        "linker": attr.label(
            allow_single_file = True,
            cfg = "exec",
        ),
        "objdump": attr.label(
            allow_single_file = True,
            cfg = "exec",
        ),
    },
    fragments = ["cpp"],
    toolchains = use_cc_toolchain(),
)
