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

"""Build a pyproject distribution with its declared PEP 517 backend."""

load("@bazel_skylib//rules:common_settings.bzl", "BuildSettingInfo")

def _python_distribution_impl(ctx):
    dist = ctx.actions.declare_directory(ctx.label.name)
    version = ctx.actions.declare_file(ctx.label.name + ".version")
    ctx.actions.write(version, ctx.attr.version[BuildSettingInfo].value)
    args = ctx.actions.args()
    args.add("--pyproject", ctx.file.pyproject)
    args.add("--version-file", version)
    args.add("--output", dist.path)
    args.add_all(ctx.files.srcs)
    ctx.actions.run(
        executable = ctx.attr._builder[DefaultInfo].files_to_run,
        arguments = [args],
        inputs = ctx.files.srcs + [ctx.file.pyproject, version],
        outputs = [dist],
        mnemonic = "PythonDistribution",
        progress_message = "Building Python distributions for %{label}",
    )
    return [
        DefaultInfo(files = depset([dist])),
        OutputGroupInfo(version = depset([version])),
    ]

python_distribution = rule(
    implementation = _python_distribution_impl,
    attrs = {
        "pyproject": attr.label(allow_single_file = [".toml"], mandatory = True),
        "srcs": attr.label_list(allow_files = True, mandatory = True),
        "version": attr.label(providers = [BuildSettingInfo], mandatory = True),
        "_builder": attr.label(
            default = Label("//tools/python_distribution:builder"),
            executable = True,
            cfg = "exec",
        ),
    },
)
