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

"""A bounded extraction experiment over declared rules_go package records."""

load("@bazel_skylib//lib:structs.bzl", "structs")
load("@io_bazel_rules_go//go:def.bzl", "GoArchive", "go_context")
load("@io_bazel_rules_go//go/tools/gopackagesdriver:aspect.bzl", "GoPkgInfo", "go_pkg_info_aspect")
load("//tools/bazeldefs:go.bzl", "go_rule")

_CompilationInfo = provider(fields = ["owner", "archives", "runtime_attributes"])
_OWNER_TAG = "go-compilation-owner:"

def _compilation_info_impl(target, ctx):
    owners = [tag[len(_OWNER_TAG):] for tag in ctx.rule.attr.tags if tag.startswith(_OWNER_TAG)]
    if len(owners) > 1:
        fail("multiple logical compilation owners: %s" % target.label)
    archive = target[GoArchive]
    archives = [archive] + [dep for dep in archive.direct if dep.data.label == archive.data.label]
    records = []
    for archive in archives:
        source = archive.source
        mode = structs.to_dict(source.mode)
        mode["pgoprofile"] = mode["pgoprofile"].path if mode["pgoprofile"] != None else None
        records.append({
            "label": str(source.label),
            "name": source.name,
            "importpath": source.importpath,
            "importmap": source.importmap,
            "pathtype": source.pathtype,
            "testfilter": source.testfilter,
            "is_main": source.is_main,
            "mode": mode,
            "srcs": [file.path for file in source.srcs],
            "embedsrcs": [file.path for file in source.embedsrcs],
            "deps": [{"label": str(dep.data.label), "importpath": dep.data.importpath} for dep in source.deps],
            "cgo": source.cgo,
            "cdeps": [str(dep.label) for dep in source.cdeps],
            "compiler_options": {key: getattr(source, key) for key in ["gc_goopts", "cppopts", "copts", "cxxopts", "clinkopts"]},
            "x_defs": source.x_defs,
        })
    return [_CompilationInfo(
        owner = owners[0] if owners else str(target.label),
        archives = records,
        runtime_attributes = {
            "data": [str(dep.label) for dep in ctx.rule.attr.data],
            "args": ctx.rule.attr.args,
            "env": ctx.rule.attr.env,
            "env_inherit": ctx.rule.attr.env_inherit,
        },
    )]

_compilation_info = aspect(implementation = _compilation_info_impl)

def _package_probe_impl(ctx):
    go = go_context(ctx)
    if go.mode.pure:
        fail("The package probe requires the existing plugin-tldk cgo configuration")
    output = ctx.actions.declare_directory(ctx.label.name)
    outcome = ctx.actions.declare_file(ctx.label.name + "-outcome.json")
    manifest = ctx.actions.declare_file(ctx.label.name + ".json")
    packages = [dep[GoPkgInfo] for dep in ctx.attr.roots]
    comparisons = [dep[GoPkgInfo] for dep in ctx.attr.comparison_roots]
    json_files = depset(transitive = [pkg.pkg_json_files for pkg in packages])
    stdlib_json = depset([pkg.stdlib_json_file for pkg in packages])
    ctx.actions.write(manifest, json.encode({
        "labels": [str(dep.label) for dep in ctx.attr.roots],
        "package_json": [f.path for f in json_files.to_list()],
        "stdlib_json": [f.path for f in stdlib_json.to_list()],
        "go_mod": ctx.file._go_mod.path,
        "goarch": go.mode.goarch,
        "goos": go.mode.goos,
        "bazel_version_file": ctx.file._bazel_version.path,
        "owner_map": {str(dep.label): dep[_CompilationInfo].owner for dep in ctx.attr.roots} if comparisons else {},
        "comparison_package_json": [file.path for file in depset(transitive = [pkg.pkg_json_files for pkg in comparisons]).to_list()],
        "compilation_metadata": {
            str(dep.label): {
                "owner": dep[_CompilationInfo].owner,
                "archives": dep[_CompilationInfo].archives,
                "runtime_attributes": dep[_CompilationInfo].runtime_attributes,
            }
            for dep in ctx.attr.roots + ctx.attr.comparison_roots
        } if comparisons else {},
    }))
    args = ctx.actions.args()
    args.add("--manifest", manifest)
    args.add("--codeql", ctx.file._codeql)
    args.add("--extractor", ctx.executable._extractor)
    args.add("--driver", ctx.executable._driver)
    args.add("--output", output.path)
    args.add("--outcome", outcome)
    args.add("--go", go.sdk.go)
    args.add("--goroot", go.sdk.root_file.dirname)
    inputs = [ctx.attr._bundle[DefaultInfo].files, json_files, stdlib_json]
    inputs.extend([pkg.pkg_json_files for pkg in comparisons])
    inputs.extend([go.sdk.srcs, go.sdk.headers, go.sdk.tools])
    for pkg in packages + comparisons:
        inputs.extend([pkg.compiled_go_files, pkg.export_files, pkg.stdlib_cache_dir])
    ctx.actions.run(
        executable = ctx.executable._runner,
        arguments = [args],
        inputs = depset([manifest, ctx.file._go_mod, ctx.file._bazel_version, go.sdk.go], transitive = inputs),
        tools = [ctx.attr._extractor[DefaultInfo].files_to_run, ctx.attr._driver[DefaultInfo].files_to_run],
        outputs = [output, outcome],
        env = go.env,
        mnemonic = "CodeQLPackageProbe",
        progress_message = "Extracting the declared Go package probe with CodeQL",
    )
    return [
        DefaultInfo(files = depset([outcome])),
        OutputGroupInfo(codeql_diagnostics = depset([output])),
    ]

package_probe = go_rule(
    rule,
    implementation = _package_probe_impl,
    attrs = {
        "roots": attr.label_list(aspects = [go_pkg_info_aspect, _compilation_info], mandatory = True),
        "comparison_roots": attr.label_list(aspects = [go_pkg_info_aspect, _compilation_info]),
        "_bazel_version": attr.label(default = "//:.bazelversion", allow_single_file = True),
        "_bundle": attr.label(default = "@codeql_bundle//:files"),
        "_codeql": attr.label(default = "@codeql_bundle//:codeql", allow_single_file = True),
        "_driver": attr.label(default = "@io_bazel_rules_go//go/tools/gopackagesdriver", executable = True, cfg = "exec"),
        "_extractor": attr.label(default = "@codeql_go_extractor//cli/go-extractor", executable = True, cfg = "exec"),
        "_go_mod": attr.label(default = "//:go.mod", allow_single_file = True),
        "_runner": attr.label(default = "//tools/codeql:package_probe_runner", executable = True, cfg = "exec"),
    },
)
