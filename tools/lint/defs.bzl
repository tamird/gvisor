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

"""Use the supplier's Python executable and main as declared action inputs."""

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
