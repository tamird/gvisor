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

load("@rules_distroless//distroless:defs.bzl", "flatten")
load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("//test/runner:defs.bzl", "runner_arm64_test")
load("//tools:defs.bzl", "bzl_library", "cc_binary", "select_gtest")
load(":defs.bzl", "arm64_tcg_test", "tcg_image")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:sandbox"],
    licenses = ["notice"],
)

sh_binary(
    name = "build_image",
    srcs = ["build_image.sh"],
    tags = ["manual"],
)

sh_binary(
    name = "run",
    testonly = True,
    srcs = ["run.sh"],
    tags = ["manual"],
)

tcg_image(
    name = "guest",
    testonly = True,
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    guest = "@tcg_arm64_guest//:flat",
    host_tools = "@tcg_host_tools//:flat",
    init = "init.sh",
    kernel_release = "6.8.0-138-generic-64k",
    tags = ["manual"],
)

# Reuse the declared userspace and its transitive dependencies without carrying
# the separate 64K kernel into the RC guest.
flatten(
    name = "rc_userspace",
    testonly = True,
    deduplicate = True,
    tags = ["manual"],
    tars = [
        "@tcg_arm64_guest//base-files/arm64",
        "@tcg_arm64_guest//base-passwd/arm64",
        "@tcg_arm64_guest//bash/arm64",
        "@tcg_arm64_guest//busybox-static/arm64",
        "@tcg_arm64_guest//coreutils/arm64",
        "@tcg_arm64_guest//debianutils/arm64",
        "@tcg_arm64_guest//file/arm64",
        "@tcg_arm64_guest//findutils/arm64",
        "@tcg_arm64_guest//grep/arm64",
        "@tcg_arm64_guest//iproute2/arm64",
        "@tcg_arm64_guest//kmod/arm64",
        "@tcg_arm64_guest//libc-bin/arm64",
        "@tcg_arm64_guest//mount/arm64",
        "@tcg_arm64_guest//netcat-openbsd/arm64",
        "@tcg_arm64_guest//procps/arm64",
        "@tcg_arm64_guest//sed/arm64",
    ],
)

tcg_image(
    name = "rc_guest",
    testonly = True,
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    guest = ":rc_userspace",
    host_tools = "@tcg_host_tools//:flat",
    init = "init.sh",
    kernel_archives = [
        "@tcg_arm64_rc_image//:data.tar",
        "@tcg_arm64_rc_modules//:data.tar",
    ],
    kernel_release = "7.3.0-070300rc3-generic",
    page_size = 4096,
    tags = ["manual"],
)

tcg_image(
    name = "amd64_rc_guest",
    testonly = True,
    architecture = "amd64",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    guest = "@kvm_amd64_guest//:flat",
    host_tools = "@kvm_host_tools//:flat",
    init = "init.sh",
    kernel_archives = [
        "@kvm_amd64_rc_image//:data.tar",
        "@kvm_amd64_rc_modules//:data.tar",
    ],
    kernel_release = "7.3.0-070300rc3-generic",
    page_size = 4096,
    tags = ["manual"],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    deps = [
        "//tools:arch_bzl",
        "//tools:defs_bzl",
        "//tools/bazeldefs:platforms_bzl",
        "@bazel_skylib//lib:shell",
    ],
)

# Fork-only native test of the emulated architecture's atomic ordering.
cc_binary(
    name = "atomic_ordering_test",
    testonly = True,
    srcs = ["atomic_ordering.cc"],
    # Inline the standard atomic wrappers inside the retained probe symbols.
    copts = ["-O2"],
    linkstatic = True,
    tags = ["manual"],
    deps = select_gtest() + [
        "//test/util:test_main",
        "//test/util:thread_util",
        "@com_google_absl//absl/base:core_headers",
    ],
)

genrule(
    name = "atomic_ordering_disassembly",
    testonly = True,
    srcs = [":atomic_ordering_test"],
    outs = ["atomic_ordering.disasm"],
    cmd = "$(location @llvm//tools:llvm-objdump) --disassemble-symbols=AtomicStoreLoad,AtomicStoreFenceLoad $(location :atomic_ordering_test) > $@",
    tags = ["manual"],
    tools = ["@llvm//tools:llvm-objdump"],
)

runner_arm64_test(
    name = "atomic_ordering_native_arm64",
    size = "medium",
    compile_exec_compatible_with = [],
    data = [":atomic_ordering_disassembly"],
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:aarch64",
    ],
    exec_properties = {
        "test.EstimatedCPU": "2",
        "test.EstimatedMemory": "2GB",
    },
    runner_args = [
        "--platform=native",
        "--one-sandbox=true",
    ],
    tags = [
        "manual",
        "no-local",
    ],
    test = ":atomic_ordering_test",
)

arm64_tcg_test(
    name = "atomic_ordering_native_64k_tcg",
    size = "medium",
    payload = ":atomic_ordering_native_arm64",
    tags = [],
)
