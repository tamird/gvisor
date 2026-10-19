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

load("@io_bazel_rules_go//go:def.bzl", "GoArchive", "go_context")
load("@io_bazel_rules_go//go/tools/gopackagesdriver:aspect.bzl", "GoPkgInfo", "go_pkg_info_aspect")
load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")
load("//tools/bazeldefs:go.bzl", "go_rule")

def _analysis_impl(ctx):
    output = ctx.actions.declare_directory(ctx.label.name)
    sarif = ctx.actions.declare_file(ctx.label.name + ".sarif")
    args = ctx.actions.args()
    args.add("--codeql", ctx.file._codeql)
    args.add("--language", ctx.attr.language)
    args.add("--output", output.path)
    args.add("--sarif", sarif.path)
    inputs = [ctx.attr._bundle[DefaultInfo].files]
    direct = []
    tools = []
    env = {}
    summaries = []
    if ctx.attr.language == "go":
        summary = ctx.actions.declare_file(ctx.label.name + "-sources.json")
        args.add("--go-summary", summary)
        summaries.append(summary)
        go = go_context(ctx)
        packages = [dep[GoPkgInfo] for dep in ctx.attr.roots]
        json_files = depset(transitive = [pkg.pkg_json_files for pkg in packages])
        stdlib_json = depset([pkg.stdlib_json_file for pkg in packages])
        compiled_inputs = depset(transitive = [pkg.compiled_go_files for pkg in packages])
        owners = []
        for dep, owner in ctx.attr.roots.items():
            if GoArchive not in dep:
                fail("Declared production owner has no GoArchive: " + owner)

            # Alias and configured frontends keep their logical owner, while
            # the driver must request the actual archive's package identity.
            owners.append({
                "owner": owner,
                "provider_label": str(dep.label),
                "package_id": str(dep[GoArchive].data.label),
            })
        if not owners:
            fail("Go analysis requires a generated nonempty production root manifest")
        manifest = ctx.actions.declare_file(ctx.label.name + "-packages.json")
        ctx.actions.write(manifest, json.encode({
            "labels": sorted({owner["package_id"]: True for owner in owners}.keys()),
            "owners": owners,
            "package_json": [file.path for file in json_files.to_list()],
            "stdlib_json": [file.path for file in stdlib_json.to_list()],
            "declared_inputs": [file.path for file in compiled_inputs.to_list()],
            "go_mod": ctx.file._go_mod.path,
            "goarch": go.mode.goarch,
            "goos": go.mode.goos,
            "bazel_version_file": ctx.file._bazel_version.path,
        }))
        args.add("--manifest", manifest)
        args.add("--go", go.sdk.go)
        args.add("--goroot", go.sdk.root_file.dirname)
        args.add("--driver", ctx.executable._driver)
        args.add("--extractor", ctx.executable._extractor)
        direct.extend([manifest, ctx.file._go_mod, ctx.file._bazel_version, go.sdk.go])
        inputs.extend([json_files, stdlib_json, compiled_inputs, go.sdk.srcs, go.sdk.headers, go.sdk.tools])
        for pkg in packages:
            # CodeQL loads dependency syntax, so the effective go/packages
            # mode does not request export archives. Retain the driver's
            # standard-library cache and its source paths.
            inputs.append(pkg.stdlib_cache_dir)
        tools.extend([ctx.attr._driver[DefaultInfo].files_to_run, ctx.attr._extractor[DefaultInfo].files_to_run])
        env.update(go.env)
    else:
        args.add("--manifest", ctx.file._manifest)
        direct.append(ctx.file._manifest)
        inputs.append(ctx.attr._sources[DefaultInfo].files)
    ctx.actions.run(
        executable = ctx.attr._runner[DefaultInfo].files_to_run,
        arguments = [args],
        inputs = depset(direct, transitive = inputs),
        tools = tools,
        outputs = [output, sarif] + summaries,
        env = env,
        mnemonic = "CodeQL",
        progress_message = "Analyzing %s sources with CodeQL" % ctx.attr.language,
    )
    return [
        DefaultInfo(files = depset([sarif])),
        OutputGroupInfo(
            codeql_diagnostics = depset([output]),
            codeql_summary = depset(summaries),
        ),
    ]

_ATTRS = {
    "language": attr.string(mandatory = True, values = ["go", "javascript", "python", "ruby"]),
    "_bundle": attr.label(default = "@codeql_bundle//:files"),
    "_codeql": attr.label(default = "@codeql_bundle//:codeql", allow_single_file = True),
    "_runner": attr.label(default = "//tools/codeql:analyze", executable = True, cfg = "exec"),
}

def _with_analysis_platform(rule):
    # Match the public Ubuntu workflow even in a mixed-platform invocation.
    return with_cfg(rule).set("cpu", "k8").set(
        "platforms",
        [Label("@io_bazel_rules_go//go/toolchain:linux_amd64_cgo")],
    )

_analysis_rule = rule(
    implementation = _analysis_impl,
    attrs = dict(_ATTRS, **{
        "_manifest": attr.label(default = "@analysis_sources//:manifest.json", allow_single_file = True),
        "_sources": attr.label(default = "@analysis_sources//:files"),
    }),
)

_analysis, _analysis_transition = _with_analysis_platform(_analysis_rule).build()  # buildifier: disable=unused-variable

_go_analysis = go_rule(
    rule,
    implementation = _analysis_impl,
    attrs = dict(_ATTRS, **{
        "roots": attr.label_keyed_string_dict(aspects = [go_pkg_info_aspect], mandatory = True),
        "_bazel_version": attr.label(default = "//:.bazelversion", allow_single_file = True),
        "_driver": attr.label(default = "@io_bazel_rules_go//go/tools/gopackagesdriver", executable = True, cfg = "exec"),
        "_extractor": attr.label(default = "@codeql_go_extractor//cli/go-extractor", executable = True, cfg = "exec"),
        "_go_mod": attr.label(default = "//:go.mod", allow_single_file = True),
    }),
)

# buildifier: disable=unused-variable
_cgo_analysis, _cgo_transition = _with_analysis_platform(_go_analysis).set(
    Label("@io_bazel_rules_go//go/config:pure"),
    False,
).build()

def codeql_analysis(name, language, roots = None, **kwargs):
    """Declare one Linux/AMD64 analysis using the public workflow's language."""
    if language == "go":
        _cgo_analysis(name = name, language = language, roots = roots, **kwargs)
    else:
        if roots != None:
            fail("Only Go analysis accepts declared package roots")
        _analysis(name = name, language = language, **kwargs)
