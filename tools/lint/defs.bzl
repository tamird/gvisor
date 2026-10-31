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

"""Helpers for running the source linters."""

load("@bazel_skylib//rules:native_binary.bzl", "native_binary")
load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("@rules_python//python:py_executable_info.bzl", "PyExecutableInfo")
load("@rules_python//python:py_runtime_info.bzl", "PyRuntimeInfo")

def _python_inputs(ctx):
    tool = ctx.attr.tool
    runtime = tool[PyRuntimeInfo]
    if not runtime.interpreter:
        fail("source lint requires a declared Python interpreter")
    return [OutputGroupInfo(
        interpreter = depset([runtime.interpreter]),
        main = depset([tool[PyExecutableInfo].main]),
        runtime = runtime.files,
    )]

python_inputs = rule(
    implementation = _python_inputs,
    attrs = {"tool": attr.label(mandatory = True, providers = [PyExecutableInfo, PyRuntimeInfo])},
)

def workspace_formatters(**formatters):
    """Expose each formatter alone and all of them through :format.

    Args:
      **formatters: Named commands with a tool, its workspace-relative arguments,
        and any additional files needed before it can run.
    """
    all_args = []
    all_data = []
    for name, formatter in formatters.items():
        target = "format_" + name
        argfile = target + ".json"
        write_file(name = target + "_args", out = argfile, content = [json.encode(formatter["args"])])
        args = ["$(rlocationpath %s)" % formatter["tool"], "$(rlocationpath :%s)" % argfile]
        data = [formatter["tool"], ":" + argfile] + formatter["data"]
        native_binary(name = target, src = ":workspace_command", args = args, data = data)
        all_args.extend(args)
        all_data.extend(data)
    native_binary(name = "format", src = ":workspace_command", args = all_args, data = all_data)
