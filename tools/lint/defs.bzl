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

"""Package-owned C/C++ formatting inputs."""

def clang_format_inputs(name):
    """Declares the package's C/C++ formatting sources.

    Args:
        name: Name of the source group.
    """
    native.filegroup(
        name = name,
        srcs = native.glob(["**/*.c", "**/*.cc", "**/*.h"], allow_empty = True),
        visibility = ["//tools/lint:__pkg__"],
    )

def _clang_format_filelist_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".txt")
    ctx.actions.write(out, "\n".join([src.path for src in ctx.files.srcs]) + "\n")
    return [DefaultInfo(files = depset([out]))]

clang_format_filelist = rule(
    implementation = _clang_format_filelist_impl,
    attrs = {"srcs": attr.label_list(allow_files = True)},
    doc = "Writes declared formatter paths without Make variable expansion.",
)
