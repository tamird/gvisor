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

"""Collect the first-party inputs which the native lint actions declare."""

load(":gazelle.bzl", "GAZELLE_PACKAGES")

def _lint_sources(ctx):
    packages = {p: True for p in GAZELLE_PACKAGES}
    ancestors = {"": True}
    for package in GAZELLE_PACKAGES:
        parts = package.split("/")
        for i in range(1, len(parts)):
            ancestors["/".join(parts[:i])] = True
    root = ctx.path(ctx.attr.root).dirname
    pending = [(root, "", False)]
    groups = {"buildifier": [], "buildifier_config": [], "cpplint": [], "cpplint_config": [], "gazelle": []}

    # A native glob stops at each subpackage; it also cannot discover a source
    # not named by any target. Visit one level per pass (Starlark has no while
    # or recursive calls) and watch directory entries for new source files.
    for _ in range(4096):
        if not pending:
            break
        level = pending
        pending = []
        for directory, prefix, inside in level:
            entries = directory.readdir(watch = "yes")
            if prefix and any([p.basename in (".git", ".hg", ".svn", "MODULE.bazel", "REPO.bazel", "WORKSPACE", "WORKSPACE.bazel") for p in entries]):
                continue
            for p in entries:
                name = p.basename
                path = prefix + name
                if name in (".git", ".hg", ".svn", "__pycache__", "node_modules") or path in ("external", "repo", ".cache/clangd") or not p.exists or (p.realpath != p and (not inside or p.is_dir)):
                    continue
                if p.is_dir:
                    pending.append((p, path + "/", inside or path in packages))
                    continue
                # Gazelle examines directory entries (including data for go:embed)
                # and applies BUILD directives inherited from parent directories.
                if inside or (name in ("BUILD", "BUILD.bazel") and prefix.rstrip("/") in ancestors) or path in (".bazelignore", "MODULE.bazel", "REPO.bazel", "WORKSPACE", "WORKSPACE.bazel", "go.mod"):
                    groups["gazelle"].append(path)
                if name in ("BUILD", "BUILD.bazel") or name.endswith(".bzl") or path in ("MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel"):
                    groups["buildifier"].append(path)
                elif path == ".buildifier.json":
                    groups["buildifier_config"].append(path)
                elif name == "CPPLINT.cfg":
                    groups["cpplint_config"].append(path)
                elif name.endswith((".c", ".cc", ".h")):
                    groups["cpplint"].append(path)
    if pending:
        fail("lint input tree exceeds 4096 directory levels")

    # Projected BUILD files are ordinary inputs, not nested Bazel packages.
    # The ROOT marker lets callers form paths without guessing the canonical
    # external repository name or its execroot layout.
    ctx.file(".bazelignore", "ROOT.d\n")
    ctx.file("ROOT", "")
    build = ['exports_files(["ROOT"])']
    groups = {name: sorted(paths) for name, paths in groups.items()}
    projected = {}
    for name, paths in groups.items():
        files = []
        for path in paths:
            dst = "ROOT.d/" + path
            if path not in projected:
                ctx.symlink(root.get_child(path), dst)
                projected[path] = True
            files.append(dst)
        build.append('filegroup(name = %r, srcs = %r, visibility = ["//visibility:public"])' % (name, files))
    ctx.file("BUILD.bazel", "\n".join(build) + "\n")
    ctx.file("inputs.bzl", "INPUTS = %s\n" % repr(groups))

lint_sources = repository_rule(
    implementation = _lint_sources,
    attrs = {"root": attr.label(default = Label("//:MODULE.bazel"))},
)
