load("//debian:artifacts.bzl", "release_inputs")
load(":boundary.bzl", "data_fixture", "tree_fixture")

# Match package writers to physical workers in this fork qualification.
[
    platform(
        name = arch,
        constraint_values = ["@llvm//constraints/libc:musl"],
        parents = ["@io_bazel_rules_go//go/toolchain:linux_%s_cgo" % arch],
        exec_properties = {
            "Arch": arch,
            "OSFamily": "linux",
            "container-image": "docker://cgr.dev/chainguard/static@sha256:fc42af33e643f20d08baf03cddcc1c02ba57052dfb02edb0d79f3fc47bcea8d9",
            "recycle-runner": "false",
            "workload-isolation-type": "oci",
        },
        visibility = ["//visibility:public"],
    )
    for arch in ["amd64", "arm64"]
]

data_fixture(name = "boundary-package", filename = "runsc.deb")
data_fixture(name = "boundary-archive", filename = "-archive, space\n")
tree_fixture(name = "boundary-tree")

release_inputs(
    name = "boundary-inputs",
    package = ":boundary-package",
    downloads = [":boundary-archive"],
    python_dist = ":boundary-tree",
)
