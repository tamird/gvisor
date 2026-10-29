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

load("@platforms//host:constraints.bzl", "HOST_CONSTRAINTS")

_BUILDIFIER_TOOLCHAIN = "@buildifier_prebuilt//buildifier:toolchain"

# The pinned supplier exposes its native executable only through this field.
# Its public binary wraps the tool in a script constrained to the Bazel host.
def _host_tool_impl(ctx):
    tool = ctx.toolchains[_BUILDIFIER_TOOLCHAIN]._tool
    return [DefaultInfo(files = depset([tool]), runfiles = ctx.runfiles(files = [tool]))]

host_tool = rule(
    implementation = _host_tool_impl,
    exec_compatible_with = HOST_CONSTRAINTS,
    toolchains = [_BUILDIFIER_TOOLCHAIN],
)

def _buildifier_impl(ctx):
    output = ctx.actions.declare_file(ctx.label.name + ".ok")
    inputs = ctx.attr.srcs[DefaultInfo].files
    tool = ctx.exec_groups["lint"].toolchains[_BUILDIFIER_TOOLCHAIN]._tool
    ctx.actions.run(
        executable = ctx.executable._driver,
        exec_group = "lint",
        arguments = ["check", tool.path, ctx.file.paths.path, output.path],
        inputs = depset([tool, ctx.file.paths], transitive = [inputs]),
        outputs = [output],
        mnemonic = "BuildifierCheck",
    )
    return [DefaultInfo(files = depset([output]))]

buildifier = rule(
    implementation = _buildifier_impl,
    exec_groups = {"lint": exec_group(toolchains = [_BUILDIFIER_TOOLCHAIN])},
    attrs = {
        "paths": attr.label(default = "@starlark_sources//:paths.json", allow_single_file = True),
        "srcs": attr.label(default = "@starlark_sources//:inputs"),
        "_driver": attr.label(default = Label("//tools/bzlformat"), executable = True, cfg = config.exec("lint")),
    },
)
