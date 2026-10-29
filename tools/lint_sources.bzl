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
    data = []
    inputs = []

    # A package's glob cannot see a subpackage. Project only the selected
    # inputs, in their original directories, into one external package.
    # Starlark supports neither recursive calls nor while loops. Process one
    # directory level per pass and fail explicitly if the depth is exceeded.
    for _ in range(4096):
        if not pending:
            break
        directories = pending
        pending = []
        for directory, relative in directories:
            entries = directory.readdir(watch = "yes")

            # Do not lint a separate checkout or repository nested in this one.
            if relative and any([p.basename in (".git", ".hg", ".svn", "MODULE.bazel", "REPO.bazel", "WORKSPACE", "WORKSPACE.bazel") for p in entries]):
                continue
            for path in entries:
                name = path.basename
                child = relative + name
                if name in (".git", ".hg", ".svn", "__pycache__", "node_modules") or child in ("external", "repo", ".cache/clangd"):
                    continue
                if not path.exists or path.realpath != path:
                    continue
                if path.is_dir:
                    pending.append((path, child + "/"))
                elif name in ctx.attr.names or name.endswith(tuple(ctx.attr.extensions)) or child in ctx.attr.root_files or child in ctx.attr.data_files:
                    ctx.symlink(path, "src/" + child)
                    inputs.append("src/" + child)
                    if child in ctx.attr.data_files:
                        data.append(child)
                    else:
                        paths.append(child)
    if pending:
        fail("lint input tree exceeds 4096 directory levels")

    # A projected BUILD is data, not a package boundary. Exclude the subtree
    # from package discovery; the root filegroup names each file explicitly.
    ctx.file(".bazelignore", "src\n")
    ctx.file("paths.json", json.encode({"data": sorted(data), "sources": sorted(paths)}))
    ctx.file("BUILD.bazel", 'exports_files(["paths.json"])\nfilegroup(name = "inputs", srcs = %s, visibility = ["//visibility:public"])\n' % repr(sorted(inputs)))

lint_sources = repository_rule(
    implementation = _sources_impl,
    attrs = {
        "data_files": attr.string_list(),
        "extensions": attr.string_list(),
        "names": attr.string_list(),
        "root": attr.label(default = Label("//:MODULE.bazel")),
        "root_files": attr.string_list(),
    },
)
