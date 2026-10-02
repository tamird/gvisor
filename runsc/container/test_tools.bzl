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

"""Declared filesystem tools for the container tests."""

def _erofs_tools_impl(ctx):
    tar = ctx.toolchains["@tar.bzl//tar/toolchain:target_type"]
    config = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(config, json.encode({
        "archive": ctx.file.src.short_path,
        "tar": tar.tarinfo.binary.short_path,
        "env": [key + "=" + tar.tarinfo.default_env[key] for key in sorted(tar.tarinfo.default_env)],
    }))
    return [DefaultInfo(
        files = depset([config]),
        runfiles = ctx.runfiles(
            files = [config, ctx.file.src],
            transitive_files = tar.default.files,
        ),
    )]

erofs_tools = rule(
    implementation = _erofs_tools_impl,
    attrs = {
        "src": attr.label(allow_single_file = True, mandatory = True),
    },
    toolchains = ["@tar.bzl//tar/toolchain:target_type"],
)
