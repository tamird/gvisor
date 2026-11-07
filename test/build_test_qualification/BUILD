load("//tools:defs.bzl", "build_test")

[
    genrule(
        name = selection + "_" + outcome + "_input",
        testonly = True,
        outs = [selection + "_" + outcome + ".built"],
        cmd = "echo intentional-build-test-input-failure >&2; exit 41" if outcome == "broken" else "echo built > $@",
    )
    for selection in ["direct", "suite"]
    for outcome in ["good", "broken"]
]

[
    build_test(
        name = "direct_" + outcome,
        targets = [":direct_" + outcome + "_input"],
    )
    for outcome in ["good", "broken"]
]

[
    build_test(
        name = "suite_" + outcome,
        size = "medium",
        targets = [":suite_" + outcome + "_input"],
    )
    for outcome in ["good", "broken"]
]

test_suite(
    name = "selected",
    tests = [":suite_good", ":suite_broken"],
)
