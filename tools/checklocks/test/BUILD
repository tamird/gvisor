load("//tools:defs.bzl", "go_library")
load("//tools/nogo:defs.bzl", "nogo_config", "nogo_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "test",
    srcs = [
        "aliases.go",
        "alignment.go",
        "anon.go",
        "atomics.go",
        "basics.go",
        "branches.go",
        "closures.go",
        "cyclic.go",
        "defer.go",
        "generics.go",
        "globals.go",
        "incompat.go",
        "inferred.go",
        "locker.go",
        "methods.go",
        "parameters.go",
        "return.go",
        "rwmutex.go",
        "test.go",
    ],
    # Disable generated marshal/state sources so they do not add unexpected
    # diagnostics to this fixture.
    marshal = False,
    stateify = False,
    deps = [
        "//pkg/atomicbitops",
        "//tools/checklocks/test/crosspkg",
    ],
)

nogo_config(
    name = "inferred_config",
    srcs = ["inferred.yaml"],
)

# Production filters suppress inference hints. Check the inference fixture
# without those filters so unexpected hints and missing expectations fail CI.
nogo_test(
    name = "inferred_nogo",
    srcs = ["inferred.go"],
    config = ":inferred_config",
    tags = ["nogo"],
    deps = [":test"],
)
