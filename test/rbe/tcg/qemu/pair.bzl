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

"""Compare guest CPU counts with one emulator and the original proc shard."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@tar.bzl//tar:tar.bzl", "tar_lib")
load("//test/rbe/tcg:defs.bzl", "TcgImageInfo")

def _pair_impl(ctx):
    if sorted(ctx.attr.order) != ["after", "before"]:
        fail("The pair must execute each QEMU arm exactly once")

    current = ctx.file._current
    image = ctx.attr.image[TcgImageInfo]
    if image.architecture != "arm64":
        fail("The RCsc diagnostic requires the original ARM64 guest")
    tar = ctx.toolchains[tar_lib.toolchain_type]
    config = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(config, json.encode({
        "order": ctx.attr.order,
        "emulator": current.short_path,
        "guest_cpus": {"before": 2, "after": 4},
        "total_shards": 9,
        "shard_index": 3,
        "launcher": ctx.executable._launcher.short_path,
        "arguments": [
            ctx.file.host_tools.short_path,
            image.kernel.short_path,
            image.initramfs.short_path,
            ctx.file.archive.short_path,
            ctx.executable.payload.short_path,
            tar.tarinfo.binary.short_path,
            str(ctx.attr.payload.label),
            "arm64_tcg",
            ctx.file._test_setup.short_path,
        ],
        "payload_args": ["--strace=false"],
    }))
    executable = ctx.actions.declare_file(ctx.label.name + ".sh")
    ctx.actions.write(executable, "#!/bin/sh\nset -eu\ncd \"$TEST_SRCDIR/$TEST_WORKSPACE\"\nexec %s %s\n" % (
        shell.quote(ctx.executable._runner.short_path),
        shell.quote(config.short_path),
    ), is_executable = True)
    files = [
        config,
        current,
        ctx.file.host_tools,
        ctx.file.archive,
        image.kernel,
        image.initramfs,
        ctx.file._test_setup,
        ctx.executable._launcher,
        ctx.executable._runner,
        tar.tarinfo.binary,
    ]
    runfiles = ctx.runfiles(files = files, transitive_files = tar.default.files)
    for target in (ctx.attr._launcher, ctx.attr._runner):
        runfiles = runfiles.merge(target[DefaultInfo].default_runfiles)
    return [DefaultInfo(executable = executable, runfiles = runfiles), testing.TestEnvironment(tar.tarinfo.default_env)]

qemu_cpu_pair_test = rule(
    implementation = _pair_impl,
    test = True,
    attrs = {
        "_current": attr.label(default = Label(":system_aarch64"), allow_single_file = True, cfg = "exec"),
        "order": attr.string_list(mandatory = True),
        "payload": attr.label(executable = True, cfg = "target", mandatory = True),
        "archive": attr.label(allow_single_file = True, mandatory = True),
        "image": attr.label(default = "//test/rbe/tcg:guest", providers = [TcgImageInfo]),
        "host_tools": attr.label(default = "@tcg_host_tools//:flat", allow_single_file = True),
        "_launcher": attr.label(default = "//test/rbe/tcg:run", executable = True, cfg = "exec"),
        "_runner": attr.label(default = Label(":run_pair"), executable = True, cfg = "target"),
        "_test_setup": attr.label(default = "@bazel_tools//tools/test:test_setup", allow_single_file = True),
    },
    toolchains = [Label(tar_lib.toolchain_type)],
)
