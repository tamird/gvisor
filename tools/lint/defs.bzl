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

"""Declared source checks and host-native formatter entrypoints."""

load("@platforms//host:constraints.bzl", "HOST_CONSTRAINTS")
load("@rules_python//python:defs.bzl", "py_binary", "py_test")
load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")

_LINUX_AMD64 = [Label("@platforms//os:linux"), Label("@platforms//cpu:x86_64")]
_GO_TOOLCHAIN = Label("@io_bazel_rules_go//go:toolchain")

def _gofmt_impl(ctx):
    # The toolchain's SDK executables belong to the execution platform, even
    # when the consuming Python program is configured for another target.
    sdk = ctx.toolchains[_GO_TOOLCHAIN].sdk
    version = [int(part) for part in sdk.version.split(".")[:2]]
    if ctx.attr.minimum and version < [1, 27]:
        tool = ctx.executable.minimum
        runfiles = ctx.attr.minimum[DefaultInfo].default_runfiles
    else:
        tools = [tool for tool in sdk.tools.to_list() if tool.basename == "gofmt"]
        if len(tools) != 1:
            fail("Go SDK %s must contain one gofmt: %s" % (sdk.version, tools))
        tool = tools[0]
        runfiles = ctx.runfiles(files = [tool])
    executable = ctx.actions.declare_file(ctx.label.name)
    ctx.actions.symlink(output = executable, target_file = tool, is_executable = True)
    return [DefaultInfo(executable = executable, runfiles = runfiles)]

_gofmt = rule(
    implementation = _gofmt_impl,
    attrs = {"minimum": attr.label(executable = True, cfg = "target")},
    executable = True,
    toolchains = [_GO_TOOLCHAIN],
)

# with_cfg returns both the callable and a transition rule that must be exported.
# buildifier: disable=unused-variable
_minimum_gofmt, _minimum_gofmt_transition = with_cfg(_gofmt).set(Label("@io_bazel_rules_go//go/toolchain:sdk_version"), "1.27.0").build()

# buildifier: disable=unused-variable
_lint_test, _lint_transition = with_cfg(py_test).set("platforms", [Label("@io_bazel_rules_go//go/toolchain:linux_amd64")]).build()

# buildifier: disable=unused-variable
_fix_binary, _fix_transition = with_cfg(py_binary).set("platforms", [Label("@platforms//host")]).build()

def gofmt_tools(name):
    """Keeps the formatter minimum independent of the module's compiler SDK.

    Args:
        name: Prefix for the check and fix formatter targets.
    """
    for suffix, constraints in [("check", _LINUX_AMD64), ("fix", HOST_CONSTRAINTS)]:
        _minimum_gofmt(
            name = name + "_minimum_" + suffix,
            exec_compatible_with = constraints,
            tags = ["manual"],
        )
        _gofmt(
            name = name + "_" + suffix + "_tool",
            exec_compatible_with = constraints,
            minimum = ":" + name + "_minimum_" + suffix,
            tags = ["manual"],
        )

def source_lint(name, tool, groups, configs = [], fix_tool = None):
    """Declares one check, and optionally its matching in-place formatter.

    Args:
        name: Public lint check name.
        tool: Declared executable for the Linux check.
        groups: Shared input manifests containing files to check.
        configs: Shared input manifests containing ancestor configuration files.
        fix_tool: Declared executable for the host-configured fix entrypoint.
    """
    args = ["--check=" + name]
    data = []
    for flag, selected in [("--sources=", groups), ("--configs=", configs)]:
        for group in selected:
            manifest = "@analysis_sources//:" + group + ".json"
            args.append(flag + "$(rlocationpath %s)" % manifest)
            data.extend([manifest, "@analysis_sources//:" + group + "_files"])
    common = dict(
        srcs = ["runner.py"],
        main = "runner.py",
        deps = ["@rules_python//python/runfiles"],
    )
    _lint_test(
        name = name,
        size = "medium",
        args = args + ["--tool=$(rlocationpath %s)" % tool],
        data = data + [tool],
        exec_compatible_with = _LINUX_AMD64,
        timeout = "long",
        **common
    )
    if fix_tool:
        _fix_binary(
            name = name + "_fix",
            args = args + ["--fix", "--tool=$(rlocationpath %s)" % fix_tool],
            data = data + [fix_tool],
            tags = ["manual"],
            **common
        )
