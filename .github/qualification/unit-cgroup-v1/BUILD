load("@bazel_skylib//rules:copy_file.bzl", "copy_file")
load("@rules_distroless//distroless:defs.bzl", "flatten")
load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("//test/rbe/tcg:defs.bzl", "amd64_kvm_test")

flatten(
    name = "erofs_tool_closure",
    deduplicate = True,
    tars = ["@container_test_tools//erofs-utils/amd64"],
)

copy_file(
    name = "erofs_archive",
    src = ":erofs_tool_closure",
    out = "erofs.tar",
    allow_symlink = True,
)

[
    sh_binary(
        name = name + "_payload",
        srcs = ["payload.sh"],
        data = [name + ".tar", name + ".inputs", ":erofs_archive"],
    )
    for name in ["container_test", "boot_test", "gofer_env_test", "gofer_maxprocs_test", "modify_test", "serialization_test"]
]

[
    amd64_kvm_test(
        name = mode + "_" + name,
        size = "large",
        payload = ":" + name + "_payload",
        args = [mode, name],
        image = "//test/rbe/tcg:amd64_cgroup_v1_guest",
        run_under = "//test/rbe:cgroup_v1" if mode == "v1" else None,
        memory_mib = 6144,
        scratch_gib = 4,
        shard_count = 8 if name == "container_test" else 1,
        guest_env = {"GO_TEST_WRAP_TESTV": "true"},
        tags = [],
    )
    for mode in ["v1", "v2"]
    for name in ["container_test", "boot_test", "gofer_env_test", "gofer_maxprocs_test", "modify_test", "serialization_test"]
]
