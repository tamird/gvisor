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

"""Check the declared Starlark sources with the configured Buildifier."""

def _native_tool_impl(ctx):
    # The supplier's public binary wraps its configured executable in a
    # shell script. Use its sole Buildifier runfile directly on bare workers.
    binaries = [f for f in ctx.attr.tool[DefaultInfo].default_runfiles.files.to_list() if f.basename in ("buildifier", "buildifier.exe")]
    if len(binaries) != 1:
        fail("expected one buildifier executable in %s, got %s" % (ctx.attr.tool.label, binaries))
    return [DefaultInfo(files = depset(binaries), runfiles = ctx.runfiles(files = binaries))]

native_tool = rule(
    implementation = _native_tool_impl,
    attrs = {"tool": attr.label(default = "@buildifier_prebuilt//:buildifier")},
)

def _buildifier_impl(ctx):
    output = ctx.actions.declare_file(ctx.label.name + ".ok")
    inputs = ctx.attr.srcs[DefaultInfo].files
    ctx.actions.run(
        executable = ctx.executable._driver,
        exec_group = "lint",
        arguments = ["check", ctx.file._tool.path, ctx.file.paths.path, output.path],
        inputs = depset([ctx.file._tool, ctx.file.paths], transitive = [inputs]),
        outputs = [output],
        mnemonic = "BuildifierCheck",
    )
    return [DefaultInfo(files = depset([output]))]

buildifier = rule(
    implementation = _buildifier_impl,
    exec_groups = {"lint": exec_group()},
    attrs = {
        "paths": attr.label(default = "@starlark_sources//:paths.json", allow_single_file = True),
        "srcs": attr.label(default = "@starlark_sources//:inputs"),
        "_driver": attr.label(default = Label("//tools/bzlformat"), executable = True, cfg = config.exec("lint")),
        "_tool": attr.label(default = Label("//tools/bzlformat:tool"), allow_single_file = True, cfg = config.exec("lint")),
    },
)
