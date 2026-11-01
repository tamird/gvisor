"""Declare real GNU runtime libraries for scratch-worker tests."""

# hermetic-llvm builds against glibc ABI stubs. Executing GNU binaries also
# requires real implementations, pinned here from the existing APT lock.
_PACKAGES = {
    "amd64": ("x86_64-linux-gnu", "79e35256227e16a607c154cdeb8d76ff12d20e31de286ea7fd9ad3b96fe0452d"),
    "arm64": ("aarch64-linux-gnu", "dd88cc85dcfbac19c0c9b606676b7b1b87f851d75afa5c830fc5ba1293a30833"),
}

def _glibc_repository(ctx):
    triple, sha256 = _PACKAGES[ctx.attr.arch]
    ctx.download_and_extract(
        url = "https://snapshot.ubuntu.com/ubuntu/20260928T000000Z/pool/main/g/glibc/libc6_2.35-0ubuntu3.15_{}.deb".format(ctx.attr.arch),
        sha256 = sha256,
        type = "deb",
    )
    ctx.extract("data.tar.zst")
    ctx.file("BUILD.bazel", """package(default_visibility = ["//visibility:public"])
exports_files(glob(["lib/{triple}/*.so.*"]))
filegroup(name = "libraries", srcs = glob(["lib/{triple}/*.so.*"]))
""".format(triple = triple))

glibc_repository = repository_rule(
    implementation = _glibc_repository,
    attrs = {"arch": attr.string(mandatory = True, values = ["amd64", "arm64"])},
)
