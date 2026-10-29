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

"""Exposes the generated files a C++ compile reads.

Used by tools/gen_compile_commands.py to avoid compiling the whole test suite
to generate compile_commands.json for clangd.
"""

load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")

def cc_compile_source(action):
    """Returns the source File used by a native C++ compile action.

    Args:
        action: An action from a configured target.

    Returns:
        The declared source File, or None for other kinds of action.
    """
    if action.mnemonic != "CppCompile":
        return None
    arguments = action.argv
    if "-c" not in arguments:
        return None
    source = arguments[arguments.index("-c") + 1]
    for f in action.inputs.to_list():
        if f.path == source:
            return f
    fail("C++ compile source is not a declared input: " + source)

def cc_compilation_headers(target, ctx):
    """Returns headers available to this target's C++ compilations.

    Args:
        target: Configured target whose compilation inputs are required.
        ctx: Aspect context for target.

    Returns:
        A depset of declared header Files, including implementation dependencies.
    """
    inputs = []
    if CcInfo in target:
        inputs.append(target[CcInfo].compilation_context.headers)

    # The public context includes private and generated headers, but excludes
    # implementation_deps. They are merged into a separate compilation context.
    # https://github.com/bazelbuild/bazel/blob/d84820503/src/main/starlark/builtins_bzl/common/cc/cc_compilation_helper.bzl#L440-L467
    inputs.extend([
        dependency[CcInfo].compilation_context.headers
        for dependency in getattr(ctx.rule.attr, "implementation_deps", [])
        if CcInfo in dependency
    ])
    return depset(transitive = inputs)

def _cc_compile_inputs_impl(target, ctx):
    inputs = [cc_compilation_headers(target, ctx)]

    # Generated sources are database entries too. Rules such as cc_proto_library
    # create them internally instead of exposing them through a srcs attribute.
    sources = []
    for action in getattr(target, "actions", []):
        source = cc_compile_source(action)
        if source != None and not source.is_source:
            sources.append(source)
    inputs.append(depset(sources))

    # Aspect propagation creates the dependency's output group; include it in
    # this group's closure so its generated sources are actually requested.
    for name in ["deps", "implementation_deps"]:
        for dependency in getattr(ctx.rule.attr, name, []):
            if OutputGroupInfo in dependency:
                inputs.append(getattr(dependency[OutputGroupInfo], "cc_compile_inputs", depset()))

    return [OutputGroupInfo(cc_compile_inputs = depset(transitive = inputs))]

# Propagates over compilation dependencies so their generated sources are built.
cc_compile_inputs = aspect(
    implementation = _cc_compile_inputs_impl,
    attr_aspects = ["deps", "implementation_deps"],
    # Include actions contributed by C++ aspects, such as cc_proto_aspect.
    # https://github.com/bazelbuild/bazel/blob/d84820503/src/main/java/com/google/devtools/build/lib/analysis/AspectCollection.java#L290-L299
    required_aspect_providers = [CcInfo],
)
