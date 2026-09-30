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
        # The old CLI analyzes repository source files, excluding generated and
        # third-party sources. Each configured compile context now gets checked;
        # choosing the first aquery entry for a source lost the other contexts.
        # Header discovery has not run yet, so action.inputs alone only contains
        # mandatory inputs. Include the declared compilation headers as well.
        headers = cc_compilation_headers(target, ctx)
        for index, action in enumerate(getattr(target, "actions", [])):
            source = cc_compile_source(action)
            if source == None or not source.is_source or source.owner.workspace_name:
                continue
            output_dir = "{}.clang_tidy/{}".format(ctx.label.name, index)
            database = ctx.actions.declare_file(output_dir + "/compile_commands.json")
            report = ctx.actions.declare_file(output_dir + "/report.txt")

            # Bazel exposes the actual, expanded compiler arguments, including
            # toolchain flags; do not reconstruct them from rule attributes.
            # https://github.com/bazelbuild/bazel/blob/d84820503/src/main/java/com/google/devtools/build/lib/rules/cpp/CppCompileAction.java#L889-L893
            ctx.actions.write(database, json.encode([{
                "file": source.path,
                "arguments": action.argv,
            }]))
            ctx.actions.run(
                executable = ctx.attr._runner[DefaultInfo].files_to_run,
                inputs = depset(
                    [database, ctx.file._config],
                    # The LLVM compilation declares its matching resource
                    # directory along with the source and other tool inputs.
                    transitive = [action.inputs, headers],
                ),
                tools = [ctx.attr._clang_tidy[DefaultInfo].files_to_run],
                outputs = [report],
                arguments = [
                    ctx.executable._clang_tidy.path,
                    database.path,
                    ctx.file._config.path,
                    report.path,
                ],
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
        "_runner": attr.label(
            default = Label("//tools/clang_tidy:run"),
            executable = True,
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
