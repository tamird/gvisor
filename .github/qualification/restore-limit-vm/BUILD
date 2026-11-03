load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("//test/rbe/tcg:defs.bzl", "amd64_kvm_test")

sh_binary(
    name = "probe",
    srcs = ["payload.sh"],
)

sh_binary(
    name = "restores",
    srcs = ["payload.sh"],
    data = ["capsule.tar"],
)

[
    amd64_kvm_test(
        name = name,
        size = "large",
        args = [name],
        guest_env = {"GO_TEST_WRAP_TESTV": "1"},
        image = "//test/rbe/tcg:amd64_cgroup_v1_guest",
        memory_mib = 6144,
        payload = payload,
        scratch_gib = 4,
        tags = [],
    )
    for name, payload in [
        ("preflight", ":probe"),
        ("restore", ":restores"),
    ]
]
