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
load("//tools:defs.bzl", "bzl_library", "pkg_tar")
load(":defs.bzl", "guest_host_tools", "tcg_image")

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

# Keep only the launcher tools and their library/firmware/notice closure. The
# package root also contains unused absolute and dangling symlinks, which cannot
# be safely materialized as a Bazel tree artifact. Selected links stay relative
# and resolve within this directory; no host filesystem paths are substituted.
_HOST_TOOL_MEMBERS = [
    "etc/mke2fs.conf",
    "lib/x86_64-linux-gnu",
    "sbin/debugfs",
    "sbin/e2fsck",
    "sbin/mke2fs",
    "usr/bin/timeout",
    "usr/bin/truncate",
    "usr/lib/x86_64-linux-gnu",
    "usr/share/common-licenses",
    "usr/share/doc",
    "usr/share/qemu",
]

guest_host_tools(
    name = "host_tools",
    testonly = True,
    archive = "@tcg_host_tools//:flat",
    members = _HOST_TOOL_MEMBERS,
    tags = ["manual"],
)

guest_host_tools(
    name = "kvm_host_tools",
    testonly = True,
    archive = "@kvm_host_tools//:flat",
    members = _HOST_TOOL_MEMBERS + [
        "usr/bin/qemu-system-x86_64",
        # The launcher selects this BIOS explicitly. The unused optionrom
        # directory contains dangling compatibility links in the package.
        "usr/share/seabios/bios-256k.bin",
    ],
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

# APT archive extraction does not create the normal mount directory or run
# alternatives/usrmerge. Supply the paths used by the fixture and container tests.
pkg_tar(
    name = "cgroup_v1_layout",
    testonly = True,
    empty_dirs = ["mnt"],
    symlinks = {
        "usr/bin/awk": "mawk",
        "usr/bin/touch": "../../bin/touch",
    },
    tags = ["manual"],
)

flatten(
    name = "cgroup_v1_userspace",
    testonly = True,
    deduplicate = True,
    tags = ["manual"],
    tars = [
        "@kvm_amd64_cgroup_v1_guest//:flat",
        ":cgroup_v1_layout",
    ],
)

tcg_image(
    name = "amd64_cgroup_v1_guest",
    testonly = True,
    architecture = "amd64",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    guest = ":cgroup_v1_userspace",
    host_tools = "@kvm_host_tools//:flat",
    init = "init.sh",
    kernel_release = "6.8.0-138-generic",
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
        "@tar.bzl//tar",
    ],
)
