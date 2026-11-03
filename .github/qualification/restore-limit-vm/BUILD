load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("//test/rbe/tcg:defs.bzl", "amd64_kvm_test")

sh_binary(
    name = "restores",
    srcs = ["payload.sh"],
    data = [
        "fixed.tar",
        "stock.tar",
    ],
)

amd64_kvm_test(
    name = "restore",
    size = "large",
    args = ["restore"],
    guest_env = {"GO_TEST_WRAP_TESTV": "1"},
    image = "//test/rbe/tcg:amd64_cgroup_v1_guest",
    memory_mib = 6144,
    payload = ":restores",
    scratch_gib = 6,
    tags = [],
)
