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

"""Generate governance files using the declared repository inputs."""

def _governance_files_impl(ctx):
    # The generator infers the repository root from areas.yaml's location.
    # Retain the full indexed directory tree, including paths outside packages.
    inputs = ctx.attr._sources[DefaultInfo].files
    for output in [ctx.outputs.codeowners, ctx.outputs.maintainers]:
        args = ctx.actions.args()
        args.add("-input", ctx.file._roster)
        args.add("-areas", ctx.file._areas)
        args.add("-format", output.basename)
        args.add("-output", output)
        ctx.actions.run(
            executable = ctx.attr._generator[DefaultInfo].files_to_run,
            arguments = [args],
            inputs = inputs,
            outputs = [output],
            mnemonic = "Governance",
            progress_message = "Generating " + output.basename,
        )

governance_files = rule(
    implementation = _governance_files_impl,
    attrs = {
        "_areas": attr.label(default = "@analysis_sources//:files/governance/areas.yaml.source", allow_single_file = True),
        "_generator": attr.label(default = "//governance/tools/maintainers:maintainers_gen", executable = True, cfg = "exec"),
        "_roster": attr.label(default = "@analysis_sources//:files/governance/maintainers.yaml.source", allow_single_file = True),
        "_sources": attr.label(default = "@analysis_sources//:files"),
    },
    outputs = {
        "codeowners": "%{name}/CODEOWNERS",
        "maintainers": "%{name}/MAINTAINERS.md",
    },
)
