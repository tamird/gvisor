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

"""Declare indexed C/C++ inputs for developer lint."""

def _git(ctx, root, args):
    index = ctx.getenv("GIT_INDEX_FILE")
    result = ctx.execute(
        [ctx.which("git"), "-C", str(root)] + args,
        environment = {"GIT_INDEX_FILE": index} if index else {},
    )
    if result.return_code:
        fail("Cannot read source lint's Git index: " + result.stderr)
    return result.stdout

def _lint_sources(ctx):
    root = ctx.path(ctx.attr.root).dirname
    control = root.get_child(".git")
    if not control.exists or ctx.which("git") == None:
        fail("Source lint requires a Git checkout and Git on PATH")
    ctx.watch(control)
    index = ctx.path(_git(ctx, root, ["rev-parse", "--path-format=absolute", "--git-path", "index"]).strip())
    if not index.exists:
        fail("Source lint requires an existing Git index")
    ctx.watch(index)
    shared = _git(ctx, root, ["rev-parse", "--path-format=absolute", "--shared-index-path"]).strip()
    if shared:
        ctx.watch(shared)
    groups = {"cc": [], "clang_format_config": []}
    selected = _git(ctx, root, ["ls-files", "--stage", "-z", "--"] + [
        "*.c", "*.cc", "*.h",
        ".clang-format", "*/.clang-format",
        "_clang-format", "*/_clang-format",
        ".clang-format-ignore", "*/.clang-format-ignore",
    ])
    for entry in selected.split("\000"):
        if not entry:
            continue
        metadata, _, name = entry.partition("\t")
        mode, _, stage = metadata.split(" ")
        if stage != "0" or mode not in ("100644", "100755"):
            fail("Source lint requires indexed regular files: " + name)
        source = root.get_child(name)
        if not source.exists or source.is_dir or source.realpath != root.realpath.get_child(name):
            fail("Source lint requires existing files without symlink aliases: " + name)
        ctx.watch(source)
        group = "cc" if name.endswith((".c", ".cc", ".h")) else "clang_format_config"
        groups[group].append(name)
    if ".clang-format" not in groups["clang_format_config"]:
        fail("Source lint requires an indexed root .clang-format")

    # Preserve adjacent style/ignore files without exposing unrelated sources.
    ctx.file(".bazelignore", "ROOT.d\n")
    ctx.file("ROOT", "")
    build = ['exports_files(["ROOT", "ROOT.d/.clang-format"])']
    for group, names in groups.items():
        files = []
        for name in names:
            dst = "ROOT.d/" + name
            ctx.symlink(root.get_child(name), dst)
            files.append(dst)
        build.append('filegroup(name = %r, srcs = %r, visibility = ["//visibility:public"])' % (group, files))
    ctx.file("BUILD.bazel", "\n".join(build) + "\n")
    ctx.file("inputs.bzl", "INPUTS = %s\nCHECKOUT = %r\n" % (repr(groups), str(root)))

lint_sources = repository_rule(
    implementation = _lint_sources,
    attrs = {"root": attr.label(default = Label("//:MODULE.bazel"))},
    local = True,
)
