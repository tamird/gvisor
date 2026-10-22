load("//tools:defs.bzl", "cc_toolchain")

package(default_applicable_licenses = ["//:license"])

genrule(
    name = "compiler_identity",
    outs = ["compiler_identity.txt"],
    cmd = "set -eu; { " +
          "printf 'compiler='; readlink -f $(CC); " +
          "$(CC) --version; " +
          "sha256sum $$(readlink -f $(CC)); " +
          "aarch64-linux-gnu-gcc --version; clang --version; " +
          "sha256sum $$(readlink -f /usr/bin/aarch64-linux-gnu-gcc) $$(readlink -f /usr/bin/clang); " +
          "dpkg-query -W gcc-11 gcc-11-aarch64-linux-gnu binutils binutils-aarch64-linux-gnu libc6-dev; " +
          "} > $@",
    tags = ["manual"],
    toolchains = [cc_toolchain],
)
