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

"""Runs clang-tidy with each configured C++ compilation's arguments and inputs."""

load("@bazel_skylib//rules/directory:providers.bzl", "DirectoryInfo")
load("@rules_cc//cc/common:cc_info.bzl", "CcInfo")
load("//tools:cc_compile_inputs.bzl", "cc_compilation_headers", "cc_compile_source")

_ClangTidyInfo = provider(
    "Reports for each configured compilation in the target's dependencies.",
    fields = {"reports": "Transitive clang-tidy reports."},
)

def _dependency_reports(ctx):
    reports = []

    # Match deps() in the compilation-database query, including dependencies
    # reached through data, implementation_deps, and custom rule attributes.
    for name in dir(ctx.rule.attr):
        value = getattr(ctx.rule.attr, name)
        if type(value) == "Target":
            dependencies = [value]
        elif type(value) == "list":
            dependencies = value
        elif type(value) == "dict":
            dependencies = value.keys() + value.values()
        else:
            continue
        reports.extend([
            dependency[_ClangTidyInfo].reports
            for dependency in dependencies
            if type(dependency) == "Target" and _ClangTidyInfo in dependency
        ])
    return reports

def _clang_tidy_impl(target, ctx):
    reports = []
    if not target.label.workspace_name:
        # Check each configured compilation: one source can use different
        # headers and flags in different targets. Generated and third-party
        # sources are maintained outside the repository's lint checks.
        # Header discovery has not run yet, so action.inputs alone only contains
        # mandatory inputs. Include the declared compilation headers as well.
        headers = cc_compilation_headers(target, ctx)
        resource_dir = ctx.attr._clang_resource_dir[DirectoryInfo]
        for index, action in enumerate(getattr(target, "actions", [])):
            source = cc_compile_source(action)
            if source == None or not source.is_source or source.owner.workspace_name:
                continue
            output_dir = "{}.clang_tidy/{}".format(ctx.label.name, index)
            report = ctx.actions.declare_file(output_dir + "/report.txt")

            # Bazel exposes the actual, expanded compiler arguments, including
            # toolchain flags; do not reconstruct them from rule attributes.
            # https://github.com/bazelbuild/bazel/blob/d84820503/src/main/java/com/google/devtools/build/lib/rules/cpp/CppCompileAction.java#L889-L893
            # Keep a report even when there are no diagnostics: clang-tidy
            # only writes --export-fixes output when there are findings.
            ctx.actions.run_shell(
                command = """
report="$1"
shift
"$@" >"$report" 2>&1 || {
    status=$?
    cat "$report" >&2
    exit "$status"
}
""",
                inputs = depset(
                    [ctx.file._config],
                    # clang-tidy needs builtin headers from its own LLVM installation.
                    transitive = [action.inputs, headers, resource_dir.transitive_files],
                ),
                tools = [ctx.attr._clang_tidy[DefaultInfo].files_to_run],
                outputs = [report],
                arguments = [
                    report.path,
                    ctx.executable._clang_tidy.path,
                    "--config-file=" + ctx.file._config.path,
                    "--quiet",
                    "--warnings-as-errors=*",
                    source.path,
                    "--",
                ] + action.argv[1:] + ["-resource-dir", resource_dir.path],
                env = action.env,
                mnemonic = "ClangTidy",
                progress_message = "Checking {} with clang-tidy".format(source.short_path),
            )
            reports.append(report)
    outputs = depset(reports, transitive = _dependency_reports(ctx))
    return [_ClangTidyInfo(reports = outputs), OutputGroupInfo(clang_tidy = outputs)]

clang_tidy = aspect(
    implementation = _clang_tidy_impl,
    attr_aspects = ["*"],
    required_aspect_providers = [CcInfo],
    attrs = {
        "_clang_resource_dir": attr.label(
            default = Label("//tools/bazeldefs/llvm:clang_resource_dir"),
            providers = [DirectoryInfo],
            cfg = "exec",
        ),
        "_clang_tidy": attr.label(
            default = Label("@llvm//tools:clang-tidy"),
            allow_single_file = True,
            executable = True,
            cfg = "exec",
        ),
        "_config": attr.label(
            default = Label("//:.clang-tidy"),
            allow_single_file = True,
        ),
    },
    provides = [_ClangTidyInfo],
)
