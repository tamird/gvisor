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

"""Collect first-party lint inputs without crossing into other repositories."""

def _sources_impl(ctx):
    pending = [(ctx.path(ctx.attr.root).dirname, "")]
    paths = []
    groups = []
    # A package's glob cannot see a subpackage. Project only the selected
    # inputs, in their original directories, with generated package metadata.
    for _ in range(4096):  # Maximum Linux path length in bytes.
        if not pending:
            break
        directories = pending
        pending = []
        for directory, relative in directories:
            entries = directory.readdir(watch = "yes")
            if relative and any([p.basename == "MODULE.bazel" for p in entries]):
                continue
            names = []
            for path in entries:
                name = path.basename
                child = relative + name
                if name in (".git", ".hg", ".svn", "__pycache__", "node_modules") or child in ("external", "repo", ".cache/clangd"):
                    continue
                if not path.exists or path.realpath != path:
                    continue
                if path.is_dir:
                    pending.append((path, child + "/"))
                elif name in ctx.attr.names or name.endswith(tuple(ctx.attr.extensions)) or child in ctx.attr.root_files:
                    ctx.symlink(path, "src/" + child)
                    names.append(name)
                    paths.append(child)
            if names:
                pkg = ("src/" + relative).removesuffix("/")
                # BUILD is a lint input; BUILD.bazel takes precedence for analysis.
                ctx.file(pkg + "/BUILD.bazel", 'filegroup(name = "inputs", srcs = %s, visibility = ["//visibility:public"])\n' % repr(sorted(names)))
                groups.append("//" + pkg + ":inputs")
    if pending:
        fail("source tree exceeds the maximum path length")
    ctx.file("paths.json", json.encode(sorted(paths)))
    ctx.file("BUILD.bazel", 'exports_files(["paths.json"])\nfilegroup(name = "inputs", srcs = %s, visibility = ["//visibility:public"])\n' % repr(sorted(groups)))

lint_sources = repository_rule(
    implementation = _sources_impl,
    attrs = {
        "extensions": attr.string_list(),
        "names": attr.string_list(),
        "root": attr.label(default = Label("//:MODULE.bazel")),
        "root_files": attr.string_list(),
    },
)
