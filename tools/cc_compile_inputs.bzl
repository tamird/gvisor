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

"""Materializes generated C++ sources and headers for compilation databases."""

load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")

# Source extensions whose generated files must be materialized.
_CC_SRC_EXTENSIONS = (".c", ".cc", ".cpp", ".cxx", ".c++", ".S", ".s", ".inc")

def _cc_compile_inputs_impl(target, ctx):
    inputs = [
        getattr(dep[OutputGroupInfo], "cc_compile_inputs", depset())
        for dep in getattr(ctx.rule.attr, "deps", [])
        if OutputGroupInfo in dep
    ]

    # Transitively covers every header the target may include, generated ones
    # included, plus the _virtual_includes symlink trees that -I flags name.
    if CcInfo in target:
        inputs.append(target[CcInfo].compilation_context.headers)

    # Generated sources may be declared as inputs or exposed as outputs, as
    # cc_proto_library does. The header set above does not include them.
    inputs.append(depset([
        f
        for f in getattr(ctx.rule.files, "srcs", []) + target[DefaultInfo].files.to_list()
        if not f.is_source and f.path.endswith(_CC_SRC_EXTENSIONS)
    ]))

    return [OutputGroupInfo(cc_compile_inputs = depset(transitive = inputs))]

# Propagates over deps so that a generated source belonging to a dependency is
# built too; headers alone would already be complete at the top-level target.
cc_compile_inputs = aspect(
    implementation = _cc_compile_inputs_impl,
    attr_aspects = ["deps"],
)
