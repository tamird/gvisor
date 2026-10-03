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

"""Declare the complete tracked source input for static analysis."""

def _git(ctx, root, args):
    git = ctx.which("git")
    if git == None:
        fail("Git is required to enumerate the tracked analysis sources")
    result = ctx.execute([git, "-C", str(root)] + args)
    if result.return_code:
        fail("Cannot enumerate analysis sources: " + result.stderr)
    return result.stdout

def _analysis_sources_impl(ctx):
    root = ctx.path(ctx.attr.root).dirname

    # The index owns membership, including newly staged files and removals.
    # Watch the actual index rather than traversing checkout/output symlinks.
    git_dir = root.get_child(".git")
    if not git_dir.is_dir:
        ctx.watch(git_dir)
    index = _git(ctx, root, ["rev-parse", "--path-format=absolute", "--git-path", "index"]).strip()
    ctx.watch(index)
    shared_index = _git(ctx, root, ["rev-parse", "--path-format=absolute", "--shared-index-path"]).strip()
    if shared_index:
        ctx.watch(shared_index)

    names = []
    for entry in _git(ctx, root, ["ls-files", "--stage", "-z"]).split("\x00"):
        if not entry:
            continue
        metadata, _, name = entry.partition("\t")
        mode, _, stage = metadata.split(" ")
        if stage != "0" or mode not in ["100644", "100755", "120000"]:
            fail("Unsupported analysis source entry: " + entry)
        source = root.get_child(name)
        ctx.watch(source)
        if not source.exists or source.is_dir:
            fail("Analysis source must be an existing file: " + name)
        if not str(source.realpath).startswith(str(root.realpath) + "/"):
            fail("Analysis source resolves outside the checkout: " + name)

        # Flat names avoid turning the source BUILD files into nested packages.
        # The action restores paths, but only needs current file contents, not
        # executable bits or symlink metadata. Files remain ordinary Bazel inputs.
        ctx.symlink(source, "files/" + str(len(names)))
        names.append(name)
    if not names:
        fail("No tracked analysis sources found")
    ctx.file("manifest.json", json.encode(names))
    ctx.file("BUILD.bazel", """
package(default_visibility = ["//visibility:public"])
exports_files(["manifest.json"])
filegroup(name = "files", srcs = glob(["files/*"]))
""")

analysis_sources = repository_rule(
    implementation = _analysis_sources_impl,
    attrs = {
        "root": attr.label(default = "//:MODULE.bazel", allow_single_file = True),
    },
    local = True,
)
