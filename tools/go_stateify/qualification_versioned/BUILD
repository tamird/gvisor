load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("//tools:defs.bzl", "go_library")

write_file(
    name = "methods",
    out = "values.go",
    content = [
        "package qualification_versioned",
        'import ("context"; "math/rand/v2"; peer "net/netip")',
        "func (*record) saveRandom() rand.PCG { return rand.PCG{} }",
        "func (*record) loadRandom(context.Context, rand.PCG) {}",
        "func (*record) saveAddress() peer.Addr { return peer.Addr{} }",
        "func (*record) loadAddress(context.Context, peer.Addr) {}",
    ],
)

go_library(
    name = "qualification_versioned",
    srcs = [
        "record.go",
        "values.go",
    ],
)
