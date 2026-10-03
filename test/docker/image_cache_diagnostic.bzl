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

"""Fork-only real-daemon validation of Make's image-cache behavior."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@rules_foreign_cc//toolchains/native_tools:tool_access.bzl", "access_tool")

def _image_cache_command_impl(ctx):
    make = access_tool(Label("@rules_foreign_cc//toolchains:make_toolchain"), ctx)
    if make.target == None or make.env != {"MAKE": make.path}:
        fail("image-cache validation requires the declared GNU Make toolchain")
    tool_root = ctx.label.name + "_tools"
    tools = {tool_root + "/" + file.path: file for file in make.target.files.to_list()}
    command = ctx.actions.declare_file(ctx.label.name)
    ctx.actions.write(command, "\n".join([
        "#!/bin/bash",
        "set -euo pipefail",
        'exec /bin/bash %s "${TEST_SRCDIR:?}/%s/%s" %s %s' % (
            shell.quote(ctx.file._script.short_path),
            tool_root,
            make.path,
            shell.quote(ctx.file._makefile.short_path),
            shell.quote(ctx.file._contexts.short_path),
        ),
        "",
    ]), is_executable = True)
    runfiles = ctx.runfiles(
        files = [ctx.file._script, ctx.file._makefile, ctx.file._contexts],
        root_symlinks = tools,
    )
    make_runfiles = make.target[DefaultInfo].default_runfiles
    if make_runfiles:
        runfiles = runfiles.merge(make_runfiles)
    return DefaultInfo(executable = command, runfiles = runfiles)

image_cache_command = rule(
    implementation = _image_cache_command_impl,
    executable = True,
    attrs = {
        "_contexts": attr.label(default = "//images:source_contexts", allow_single_file = True),
        "_makefile": attr.label(default = "//tools:images.mk", allow_single_file = True),
        "_script": attr.label(default = "//test/docker:image_cache_diagnostic.sh", allow_single_file = True),
    },
    toolchains = ["@rules_foreign_cc//toolchains:make_toolchain"],
)
