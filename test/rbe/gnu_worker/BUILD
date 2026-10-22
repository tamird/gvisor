load("//tools:defs.bzl", "cc_toolchain")

package(default_applicable_licenses = ["//:license"])

genrule(
    name = "compiler_identity",
    outs = ["compiler_identity.txt"],
    cmd = "set -eu; { " +
          "printf 'compiler='; readlink -f $(CC); " +
          "$(CC) --version; " +
          "sha256sum $$(readlink -f $(CC)); " +
          "dpkg-query -W gcc-11 binutils libc6-dev; " +
          "} > $@",
    tags = ["manual"],
    toolchains = [cc_toolchain],
)
