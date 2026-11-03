load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("//test/rbe/tcg:defs.bzl", "amd64_kvm_test")

sh_binary(
    name = "payload",
    srcs = ["payload.sh"],
    data = ["hook.bash", "selected-test"],
)

amd64_kvm_test(
    name = "guest_test",
    size = "medium",
    guest_env = {
        "GO_TEST_WRAP_TESTV": "true",
        "GVISOR_CGROUP_TEST_USER": "nobody",
        "GVISOR_QUALIFICATION_TOPOEXT": "false",
    },
    image = "//test/rbe/tcg:amd64_cgroup_v1_guest",
    memory_mib = 1536,
    payload = ":payload",
    run_under = "//test/rbe:cgroup_v1",
    scratch_gib = 1,
    smt = True,
    tags = [],
)

amd64_kvm_test(
    name = "guest_topoext_test",
    size = "medium",
    guest_env = {
        "GO_TEST_WRAP_TESTV": "true",
        "GVISOR_CGROUP_TEST_USER": "nobody",
        "GVISOR_QUALIFICATION_TOPOEXT": "true",
    },
    image = "//test/rbe/tcg:amd64_cgroup_v1_guest",
    memory_mib = 1536,
    payload = ":payload",
    run_under = "//test/rbe:cgroup_v1",
    scratch_gib = 1,
    smt = True,
    tags = [],
)
