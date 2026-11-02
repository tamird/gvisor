load("@bazel_skylib//rules:native_binary.bzl", "native_test")

# The ordinary native syscall runner maps only ID zero into its user
# namespace. This control must change to the distinct scratch UID and GID.
native_test(
    name = "ownership_test",
    size = "small",
    src = "//test/syscalls/linux:shm_test",
    out = "ownership_test",
)
