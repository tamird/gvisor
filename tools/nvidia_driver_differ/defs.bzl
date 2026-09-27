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

"""NVIDIA ABI extraction using declared sources and the target C++ toolchain."""

load("@rules_cc//cc:action_names.bzl", "ACTION_NAMES")
load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain", "use_cc_toolchain")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")

def _extract_resources(_os, _inputs):
    # Parse translation units sequentially, with one Clang AST live at a time.
    return {"cpu": 1, "memory": 1024}

def _extract_impl(ctx):
    toolchain = find_cc_toolchain(ctx)
    features = cc_common.configure_features(
        ctx = ctx,
        cc_toolchain = toolchain,
        requested_features = ctx.features,
        unsupported_features = ctx.disabled_features,
    )
    variables = cc_common.create_compile_variables(
        feature_configuration = features,
        cc_toolchain = toolchain,
        user_compile_flags = ctx.fragments.cpp.copts + ctx.fragments.cpp.cxxopts,
    )
    compiler_args = cc_common.get_memory_inefficient_command_line(
        feature_configuration = features,
        action_name = ACTION_NAMES.cpp_compile,
        variables = variables,
    )

    # ClangTool otherwise derives resources from the parser executable. The
    # selected LLVM toolchain supplies this flag and its directory as an input.
    resource_dir = ""
    for i, arg in enumerate(compiler_args):
        if arg.startswith("-resource-dir="):
            resource_dir = arg.removeprefix("-resource-dir=")
        elif arg == "-resource-dir":
            resource_dir = compiler_args[i + 1] if i + 1 < len(compiler_args) else ""
    if not resource_dir or resource_dir.startswith("-"):
        fail("NVIDIA ABI extraction requires a toolchain with an explicit Clang resource directory")

    output = ctx.actions.declare_file(ctx.label.name + ".json")
    args = ctx.actions.args()
    args.add("--parser", ctx.executable._parser)
    args.add("--source", ctx.file.root.dirname)
    args.add("--version", ctx.attr.version)
    args.add("--output", output)
    args.add("--")
    args.add(cc_common.get_tool_for_action(
        feature_configuration = features,
        action_name = ACTION_NAMES.cpp_compile,
    ))
    args.add_all(compiler_args)
    ctx.actions.run(
        executable = ctx.attr._extractor[DefaultInfo].files_to_run,
        arguments = [args],
        inputs = depset(ctx.files.srcs + [ctx.file.root], transitive = [toolchain.all_files]),
        tools = [ctx.attr._parser[DefaultInfo].files_to_run],
        outputs = [output],
        env = cc_common.get_environment_variables(
            feature_configuration = features,
            action_name = ACTION_NAMES.cpp_compile,
            variables = variables,
        ),
        mnemonic = "NvidiaDriverABI",
        resource_set = _extract_resources,
        progress_message = "Extracting NVIDIA driver {} ABI for {}".format(ctx.attr.version, ctx.label),
    )
    return [DefaultInfo(files = depset([output]))]

_extract = rule(
    implementation = _extract_impl,
    attrs = {
        "srcs": attr.label(mandatory = True),
        "root": attr.label(mandatory = True, allow_single_file = True),
        "version": attr.string(mandatory = True),
        "_extractor": attr.label(
            default = Label("//tools/nvidia_driver_differ/extract:extract"),
            cfg = "exec",
            executable = True,
        ),
        "_parser": attr.label(
            default = Label("//tools/nvidia_driver_differ:driver_ast_parser"),
            cfg = "exec",
            executable = True,
        ),
    },
    fragments = ["cpp"],
    toolchains = use_cc_toolchain(),
)

def _manifest_impl(ctx):
    sources = {version: None for version in ctx.attr.unavailable}
    files = []
    for target, version in ctx.attr.drivers.items():
        outputs = target[DefaultInfo].files.to_list()
        if len(outputs) != 1:
            fail("{} must produce exactly one ABI JSON file".format(target.label))
        output = outputs[0]
        sources[version] = output.short_path
        files.append(output)
    manifest = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(manifest, json.encode(sources))
    files.append(manifest)
    return [DefaultInfo(files = depset(files), runfiles = ctx.runfiles(files = files))]

_manifest = rule(
    implementation = _manifest_impl,
    attrs = {
        "drivers": attr.label_keyed_string_dict(),
        "unavailable": attr.string_list(),
    },
)

def driver_abi(name, sources, **kwargs):
    """Extract supported driver ABIs and index their declared test data.

    Args:
      name: The index and data target name.
      sources: Generated version-to-repository map; None means no public tag.
      **kwargs: Common attributes for the index target.
    """
    drivers = {}
    unavailable = []
    for version, repo in sources.items():
        if repo == None:
            unavailable.append(version)
            continue
        target = name + "_" + version.replace(".", "_")
        _extract(
            name = target,
            srcs = repo + "//:sources",
            # COPYING anchors the root of the complete unpacked source tree.
            root = repo + "//:COPYING",
            version = version,
        )
        drivers[":" + target] = version
    _manifest(
        name = name,
        drivers = drivers,
        unavailable = unavailable,
        **kwargs
    )
