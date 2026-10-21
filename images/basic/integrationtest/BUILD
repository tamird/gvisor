load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//tools/bazeldefs:cc.bzl", "cc_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

cc_binary(
    name = "rewinddir_asan",
    testonly = True,
    srcs = ["test_rewinddir.c"],
    copts = [
        "-O1",
        "-g",
        "-fsanitize=address",
        "-fno-omit-frame-pointer",
    ],
    linkopts = ["-fsanitize=address"],
    target_compatible_with = ["@platforms//os:linux"],
)

sh_test(
    name = "rewinddir_asan_test",
    srcs = ["rewinddir_asan_test.sh"],
    args = ["$(rootpath :rewinddir_asan)"],
    data = [":rewinddir_asan"],
    env = {"ASAN_OPTIONS": "detect_leaks=0"},
    target_compatible_with = ["@platforms//os:linux"],
)
