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

"""Declare selected source inputs with their current contents."""

def _git(ctx, root, args):
    git = ctx.which("git")
    if git == None:
        fail("Git is required to select source inputs")
    result = ctx.execute([git, "-C", str(root)] + args)
    if result.return_code:
        fail("Cannot select source inputs: " + result.stderr)
    return result.stdout

def _mirror_sources(ctx, root, names):
    # A suffix keeps source BUILD files from defining Bazel packages while
    # stable paths avoid invalidating every input when membership changes.
    mirrored = ["files/" + name + ".source" for name in names]
    mirrors = {name: True for name in mirrored}
    for name, mirror in zip(names, mirrored):
        parts = mirror.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in mirrors:
                fail("Source file/directory collision after adding .source: " + name)
        source = root.get_child(name)

        # Declared remote inputs must not upload targets outside the checkout.
        if not str(source.realpath).startswith(str(root.realpath) + "/"):
            fail("Source resolves outside the checkout: " + name)
        ctx.symlink(source, mirror)

    return mirrored

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
    for entry in _git(ctx, root, ["ls-files", "--stage", "-z"]).split("\000"):
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

        names.append(name)
    if not names:
        fail("No tracked analysis sources found")
    mirrored = _mirror_sources(ctx, root, names)

    ctx.file("manifest.json", json.encode(names))
    exports = ["manifest.json"] + mirrored
    ctx.file("BUILD.bazel", "\n".join([
        "package(default_visibility = %s)" % repr(["//visibility:public"]),
        "exports_files(%s)" % repr(exports),
        "filegroup(name = %s, srcs = %s)" % (repr("files"), repr(mirrored)),
    ]) + "\n")

analysis_sources = repository_rule(
    implementation = _analysis_sources_impl,
    attrs = {
        "root": attr.label(default = "//:MODULE.bazel", allow_single_file = True),
    },
    local = True,
)

def _added_sources_impl(ctx):
    root = ctx.path(ctx.attr.root).dirname
    base = ctx.getenv("GVISOR_HEADER_BASE", "")
    head = ctx.getenv("GVISOR_HEADER_HEAD", "")
    names = []
    if base or head:
        for revision in [base, head]:
            resolved = _git(ctx, root, ["rev-parse", "--verify", "--end-of-options", revision + "^{commit}"]).strip()
            if revision != resolved:
                fail("License header inputs require full commit IDs for GVISOR_HEADER_BASE and GVISOR_HEADER_HEAD")

        # The public header policy excludes renamed files. Make the standard
        # similarity threshold explicit instead of inheriting diff.renames.
        names = [name for name in _git(ctx, root, [
            "diff",
            "--name-only",
            "--diff-filter=A",
            "--find-renames=50%",
            "-z",
            base + "..." + head,
            "--",
        ]).split("\000") if name]

    present = []
    for name in names:
        source = root.get_child(name)

        # A deleted added file is skipped by the policy. Watch it first so
        # restoring that path invalidates a previous skipped result.
        ctx.watch(source)
        if source.exists and not source.is_dir:
            present.append(name)
    mirrored = _mirror_sources(ctx, root, present)

    # Empty revision fields let ordinary graph queries load this manual test.
    # The checker rejects them before checking any file, rather than passing an
    # unconfigured comparison as an empty added-file set.
    ctx.file("manifest", "\000".join([base, head] + present) + "\000")
    ctx.file("BUILD.bazel", "\n".join([
        "package(default_visibility = %s)" % repr(["//visibility:public"]),
        "exports_files(%s)" % repr(["manifest"] + mirrored),
        "filegroup(name = %s, srcs = %s)" % (repr("files"), repr(mirrored)),
    ]) + "\n")

added_sources = repository_rule(
    implementation = _added_sources_impl,
    attrs = {
        "root": attr.label(default = "//:MODULE.bazel", allow_single_file = True),
    },
    local = True,
)
