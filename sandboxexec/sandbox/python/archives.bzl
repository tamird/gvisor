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

"""Build the SandboxExec release using the registered Python interpreter."""

load("@bazel_skylib//rules:common_settings.bzl", "BuildSettingInfo")

def _release_archives_impl(ctx):
    runtime = ctx.toolchains["@rules_python//python:toolchain_type"].py3_runtime
    dist = ctx.actions.declare_directory(ctx.label.name)
    args = ctx.actions.args()
    args.add_all(["-B", ctx.file._builder, ctx.file._backend, ctx.file.project, dist, ctx.attr._version[BuildSettingInfo].value])
    args.add_all(ctx.files.srcs)
    ctx.actions.run(
        executable = runtime.interpreter,
        arguments = [args],
        inputs = depset(
            [ctx.file._builder, ctx.file._backend, ctx.file.project] + ctx.files.srcs,
            transitive = [runtime.files],
        ),
        outputs = [dist],
        env = {"SOURCE_DATE_EPOCH": "315532800"},
        mnemonic = "SandboxExecDist",
        toolchain = "@rules_python//python:toolchain_type",
    )
    return [DefaultInfo(files = depset([dist]))]

release_archives = rule(
    implementation = _release_archives_impl,
    attrs = {
        "project": attr.label(mandatory = True, allow_single_file = True),
        "srcs": attr.label_list(allow_files = True),
        "_backend": attr.label(default = "@sandboxexec_flit_core//file", allow_single_file = True),
        "_builder": attr.label(default = ":build_dist.py", allow_single_file = True),
        "_version": attr.label(default = ":dist_version"),
    },
    toolchains = ["@rules_python//python:toolchain_type"],
)
