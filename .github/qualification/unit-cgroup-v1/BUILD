load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("//test/rbe/tcg:defs.bzl", "amd64_kvm_test")

# check.py supplies two original inputs from the independent native-unit source.
sh_binary(
    name = "payload",
    testonly = True,
    srcs = ["payload.sh"],
    data = ["hook.bash", "selected-test"],
)

amd64_kvm_test(
    name = "guest_test",
    size = "medium",
    guest_env = {
        "GO_TEST_WRAP_TESTV": "1",
        "GVISOR_CGROUP_TEST_USER": "nobody",
    },
    image = "//test/rbe/tcg:amd64_cgroup_v1_guest",
    memory_mib = 1536,
    payload = ":payload",
    run_under = "//test/rbe:cgroup_v1",
    scratch_gib = 1,
    tags = [],
)
