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

"""The exact Ubuntu QEMU source used by the bounded RCsc diagnostic."""

def _qemu_source_impl(ctx):
    ctx.download_and_extract(
        url = "https://snapshot.ubuntu.com/ubuntu/20260928T000000Z/pool/main/q/qemu/qemu_6.2+dfsg.orig.tar.xz",
        sha256 = "470d4202ab6302f184bf934008d2306c144d3221aa3b8ceb8a710414755af61b",
        stripPrefix = "qemu-6.2+dfsg",
    )
    ctx.download_and_extract(
        url = "https://snapshot.ubuntu.com/ubuntu/20260928T000000Z/pool/main/q/qemu/qemu_6.2+dfsg-2ubuntu6.31.debian.tar.xz",
        sha256 = "97dd9f9b065452e02b00f87e70e94d9f6369716de7d739ae605240d2522183a2",
    )

    # GNU patch applies the original quilt series inside the declared build
    # action. Bazel's native patcher does not accept every distro patch form.
    ctx.file("qemu-source.json", json.encode({
        "version": "1:6.2+dfsg-2ubuntu6.31",
        "origSHA256": "470d4202ab6302f184bf934008d2306c144d3221aa3b8ceb8a710414755af61b",
        "debianSHA256": "97dd9f9b065452e02b00f87e70e94d9f6369716de7d739ae605240d2522183a2",
    }) + "\n")
    ctx.symlink(ctx.attr.build_file, "BUILD.bazel")

qemu_source = repository_rule(
    implementation = _qemu_source_impl,
    attrs = {"build_file": attr.label(allow_single_file = True, mandatory = True)},
)
