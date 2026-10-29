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

"""Run the pinned Codespell wheel on the selected worker's Python runtime."""

load("@lint_deps//:requirements.bzl", "requirement")
load("@rules_python//python:py_info.bzl", "PyInfo")

_PYTHON = "@bazel_tools//tools/python:toolchain_type"

def _spelling_impl(ctx):
    runtime = ctx.exec_groups["lint"].toolchains[_PYTHON].py3_runtime
    if not runtime.interpreter:
        fail("codespell requires a file-backed Python runtime")

    # Invoke the public python -m codespell_lib module without a shell launcher.
    # The wheel's runfiles also include the dictionaries it opens alongside it.
    wheel = ctx.attr._wheel
    sources = wheel[PyInfo].transitive_original_sources
    mains = [f for f in sources.to_list() if f.path.endswith("/codespell_lib/__main__.py")]
    if len(mains) != 1:
        fail("the Codespell wheel must contain exactly one codespell_lib/__main__.py")
    output = ctx.actions.declare_file(ctx.label.name + ".ok")
    ctx.actions.run(
        executable = runtime.interpreter,
        exec_group = "lint",
        arguments = ["-IB", ctx.file._driver.path, mains[0].path, ctx.file._paths.path, output.path],
        inputs = depset([ctx.file._driver, ctx.file._paths], transitive = [
            runtime.files,
            sources,
            wheel[DefaultInfo].default_runfiles.files,
            ctx.attr._inputs[DefaultInfo].files,
        ]),
        outputs = [output],
        mnemonic = "CodespellCheck",
    )
    return [DefaultInfo(files = depset([output]))]

spelling = rule(
    implementation = _spelling_impl,
    exec_groups = {"lint": exec_group(toolchains = [_PYTHON])},
    attrs = {
        "_driver": attr.label(default = Label("//tools/lint:spelling.py"), allow_single_file = True),
        "_inputs": attr.label(default = Label("@spelling_sources//:inputs")),
        "_paths": attr.label(default = Label("@spelling_sources//:paths.json"), allow_single_file = True),
        "_wheel": attr.label(default = requirement("codespell"), cfg = config.exec("lint"), providers = [PyInfo]),
    },
)
