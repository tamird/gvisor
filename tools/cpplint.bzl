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

"""One cpplint action over the first-party source tree, across Bazel packages."""

load("@rules_python//python:py_executable_info.bzl", "PyExecutableInfo")
load("@rules_python//python:py_runtime_info.bzl", "PyRuntimeInfo")

def _cpplint_sources_impl(ctx):
    # Native glob stops at BUILD files. Project only lint inputs, preserving
    # directory names so cpplint finds the nearest CPPLINT.cfg. Like glob,
    # this includes new source files before they are committed.
    pending = [(ctx.path(ctx.attr.root).dirname, "")]
    for _ in range(4096):  # Maximum Linux path length, in bytes.
        if not pending:
            break
        directories = pending
        pending = []
        for directory, relative in directories:
            entries = directory.readdir(watch = "yes")
            if relative and any([p.basename == "MODULE.bazel" for p in entries]):
                continue
            for path in entries:
                name = path.basename
                if name.startswith(".") or name in ("__pycache__", "node_modules"):
                    continue
                if not relative and (name.startswith("bazel-") or name in ("external", "repo")):
                    continue
                child = relative + name
                if path.is_dir:
                    if path.realpath == path:  # Don't follow directory symlinks outside the tree.
                        pending.append((path, child + "/"))
                elif (name == "CPPLINT.cfg" or name.endswith((".c", ".cc", ".h"))) and path.exists and path.realpath == path:
                    ctx.symlink(path, "src/" + child)
    if pending:
        fail("cpplint source tree exceeds the maximum path length")
    ctx.file("src/ROOT", "")
    ctx.file("BUILD.bazel", """
exports_files(["src/ROOT"])
filegroup(name = "inputs", srcs = glob(["src/**"], exclude = ["src/ROOT"]), visibility = ["//visibility:public"])
""")

cpplint_sources = repository_rule(
    implementation = _cpplint_sources_impl,
    attrs = {"root": attr.label(default = Label("//:MODULE.bazel"))},
)

def _source_name(file):
    if file.basename != "CPPLINT.cfg":
        return file.owner.name.removeprefix("src/")
    return None

def _cpplint_impl(ctx):
    tool = ctx.attr._tool
    runtime = tool[PyRuntimeInfo]
    if not runtime.interpreter:
        fail("cpplint requires a declared Python interpreter")
    # cpplint is one script using only the standard library. Run it with its
    # configured interpreter directly, without the py_binary's shell bootstrap.
    main = tool[PyExecutableInfo].main
    inputs = ctx.attr._inputs[DefaultInfo].files
    output = ctx.actions.declare_file(ctx.label.name + ".ok")
    args = ctx.actions.args()
    args.add_all(["-IB", ctx.file._driver, output, main, ctx.file._root])
    args.add_all(inputs, map_each = _source_name)
    ctx.actions.run(
        executable = runtime.interpreter,
        inputs = depset([ctx.file._driver, main, ctx.file._root], transitive = [runtime.files, inputs]),
        outputs = [output],
        arguments = [args],
        mnemonic = "Cpplint",
    )
    return [DefaultInfo(files = depset([output]))]

cpplint = rule(
    implementation = _cpplint_impl,
    attrs = {
        "_driver": attr.label(default = Label("//tools:cpplint.py"), allow_single_file = True),
        "_inputs": attr.label(default = Label("@cpplint_sources//:inputs")),
        "_root": attr.label(default = Label("@cpplint_sources//:src/ROOT"), allow_single_file = True),
        "_tool": attr.label(default = Label("@cpplint//:cpplint"), cfg = "exec", providers = [PyExecutableInfo, PyRuntimeInfo]),
    },
)
