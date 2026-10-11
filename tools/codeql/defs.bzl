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

"""Run CodeQL extraction and the bundled default queries as a Bazel action."""

load("@io_bazel_rules_go//go:def.bzl", "go_context")
load("@rules_cc//cc:action_names.bzl", "ACTION_NAMES")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")
load("//tools/bazeldefs:go.bzl", "go_rule")

def _analysis_impl(ctx):
    output = ctx.actions.declare_directory(ctx.label.name)
    sarif = ctx.actions.declare_file(ctx.label.name + ".sarif")
    args = ctx.actions.args()
    args.add("--codeql", ctx.file._codeql)
    args.add("--manifest", ctx.file._manifest)
    args.add("--language", ctx.attr.language)
    args.add("--output", output.path)
    args.add("--sarif", sarif.path)
    inputs = [ctx.attr._bundle[DefaultInfo].files, ctx.attr._sources[DefaultInfo].files]
    direct = [ctx.file._manifest]
    env = {}
    if ctx.attr.language == "go":
        go = go_context(ctx)
        if go.cgo_tools == None:
            fail("CodeQL Go extraction requires the Linux C toolchain")
        cc = go.cgo_tools
        args.add("--go", go.sdk.go)
        args.add("--goroot", go.sdk.root_file.dirname)
        args.add("--proxy", ctx.file._proxy_root.dirname)
        args.add("--go-mod", ctx.file._go_mod)
        args.add("--go-sum", ctx.file._go_sum)
        args.add("--go-sources", ctx.file._go_sources)
        args.add("--cc", cc.c_compiler_path)
        args.add("--cxx", cc_common.get_tool_for_action(
            feature_configuration = cc.feature_configuration,
            action_name = ACTION_NAMES.cpp_compile,
        ))
        args.add_all(cc.c_compile_options, format_each = "--cflag=%s")
        args.add_all(cc.cxx_compile_options, format_each = "--cxxflag=%s")
        args.add_all(cc.ld_executable_options, format_each = "--ldflag=%s")
        direct.extend([go.sdk.go, ctx.file._proxy_root, ctx.file._go_mod, ctx.file._go_sum, ctx.file._go_sources])
        inputs.extend([go.sdk.srcs, go.sdk.headers, go.sdk.tools, go.cc_toolchain_files, ctx.attr._proxy[DefaultInfo].files])
        env.update(go.env)
    ctx.actions.run(
        executable = ctx.attr._runner[DefaultInfo].files_to_run,
        arguments = [args],
        inputs = depset(direct, transitive = inputs),
        outputs = [output, sarif],
        env = env,
        mnemonic = "CodeQL",
        progress_message = "Analyzing %s sources with CodeQL" % ctx.attr.language,
    )
    return [
        DefaultInfo(files = depset([sarif])),
        OutputGroupInfo(codeql_diagnostics = depset([output])),
    ]

_ATTRS = {
    "language": attr.string(mandatory = True, values = ["go", "javascript", "python", "ruby"]),
    "_bundle": attr.label(default = "@codeql_bundle//:files"),
    "_codeql": attr.label(default = "@codeql_bundle//:codeql", allow_single_file = True),
    "_manifest": attr.label(default = "@analysis_sources//:manifest.json", allow_single_file = True),
    "_runner": attr.label(default = "//tools/codeql:analyze", executable = True, cfg = "exec"),
    "_sources": attr.label(default = "@analysis_sources//:files"),
}

def _with_analysis_platform(rule):
    # Match the public Ubuntu workflow even in a mixed-platform invocation.
    return with_cfg(rule).set("cpu", "k8").set(
        "platforms",
        [Label("@io_bazel_rules_go//go/toolchain:linux_amd64_cgo")],
    )

_analysis_rule = rule(implementation = _analysis_impl, attrs = _ATTRS)

# with_cfg's generated forwarding rules must be exported from this module.
_analysis, _analysis_transition = _with_analysis_platform(_analysis_rule).build()  # buildifier: disable=unused-variable

_go_analysis = go_rule(
    rule,
    implementation = _analysis_impl,
    attrs = dict(_ATTRS, **{
        "_go_mod": attr.label(default = "@codeql_go_modules//:module/go.mod", allow_single_file = True),
        "_go_sum": attr.label(default = "@codeql_go_modules//:module/go.sum", allow_single_file = True),
        "_go_sources": attr.label(default = "//:go_export", allow_single_file = True),
        "_proxy": attr.label(default = "@codeql_go_modules//:files"),
        "_proxy_root": attr.label(default = "@codeql_go_modules//:modules/cache/download/ROOT", allow_single_file = True),
    }),
)

# buildifier: disable=unused-variable
_cgo_analysis, _cgo_transition = _with_analysis_platform(_go_analysis).set(
    Label("@io_bazel_rules_go//go/config:pure"),
    False,
).build()

def codeql_analysis(name, language, **kwargs):
    """Declare one Linux/AMD64 analysis using the public workflow's language."""
    implementation = _cgo_analysis if language == "go" else _analysis
    implementation(name = name, language = language, **kwargs)
