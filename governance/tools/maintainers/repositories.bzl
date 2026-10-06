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

"""Expose source directory names without making file contents build inputs."""

def _source_directories_impl(ctx):
    root = ctx.path(ctx.attr.root).dirname
    find = ctx.which("find")
    if find == None:
        fail("find is required to enumerate source directories")

    # Do not follow symlinks: in particular, bazel-* links lead into build outputs.
    result = ctx.execute([find, str(root), "-name", ".git", "-prune", "-o", "-type", "d", "-print0"])
    if result.return_code:
        fail("Cannot enumerate source directories: " + result.stderr)
    directories = []
    for name in result.stdout.split("\000"):
        if not name:
            continue
        directory = ctx.path(name)
        # Entry watches detect renamed/added/removed directories without watching
        # file contents. Directory type changes must invalidate the list too.
        directory.readdir(watch = "yes")
        ctx.watch(directory)
        if directory != root:
            directories.append(name.removeprefix(str(root)))
    ctx.file("directories.json", json.encode(sorted(directories)))
    ctx.file("BUILD.bazel", 'exports_files(["directories.json"])\n')

source_directories = repository_rule(
    implementation = _source_directories_impl,
    attrs = {
        "root": attr.label(default = "//:MODULE.bazel", allow_single_file = True),
    },
    local = True,
)
