load("@rules_shell//shell:sh_test.bzl","sh_test")
sh_test(
    name="symlink_test",
    srcs=["run_binary_find_runfiles_test.sh"],
    data=[":probe","@bazel_tools//tools/bash/runfiles"],
    env={"BIN_RLOCATION":"$(rlocationpath :probe)"},
)
