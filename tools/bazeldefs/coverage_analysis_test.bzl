"""Analysis tests for generated Go coverage selection."""

load("@io_bazel_rules_go//go:def.bzl", "GoInfo")

_PACKAGE = "//tools/bazeldefs/tests/coverage:"

def _coverage_configs_impl(_settings, _attr):
    return {
        "ordinary": {
            "//command_line_option:collect_code_coverage": False,
            "//command_line_option:instrumentation_filter": "^//",
        },
        "own": {
            "//command_line_option:collect_code_coverage": True,
            "//command_line_option:instrumentation_filter": "^" + _PACKAGE + "own$",
        },
        "embedded": {
            "//command_line_option:collect_code_coverage": True,
            "//command_line_option:instrumentation_filter": "^" + _PACKAGE + "embedded$",
        },
        "both": {
            "//command_line_option:collect_code_coverage": True,
            "//command_line_option:instrumentation_filter": "^" + _PACKAGE,
        },
        "excluded": {
            "//command_line_option:collect_code_coverage": True,
            "//command_line_option:instrumentation_filter": "^//,-^" + _PACKAGE,
        },
    }

_coverage_configs = transition(
    implementation = _coverage_configs_impl,
    inputs = [],
    outputs = [
        "//command_line_option:collect_code_coverage",
        "//command_line_option:instrumentation_filter",
    ],
)

def _paths(files):
    return sorted([file.short_path for file in files])

def _coverage_selection_test_impl(ctx):
    errors = []
    expected_configs = ["both", "embedded", "excluded", "ordinary", "own"]
    if sorted(ctx.split_attr.targets.keys()) != expected_configs:
        errors.append("configuration cases = %s, want %s" % (sorted(ctx.split_attr.targets.keys()), expected_configs))
    for config, targets in ctx.split_attr.targets.items():
        own, embedded = [target[GoInfo] for target in targets]
        own_sources = [file for file in own.srcs if file not in embedded.srcs]
        if not own_sources or not all([file.basename.endswith(".pb.go") for file in own_sources]):
            errors.append("%s: expected generated protobuf sources, got %s" % (config, _paths(own_sources)))
        if len(embedded.srcs) != 1 or embedded.srcs[0].basename != "embedded.go":
            errors.append("%s: unexpected embedded sources %s" % (config, _paths(embedded.srcs)))
        if any([file not in own.srcs for file in embedded.srcs]):
            errors.append("%s: generated library dropped embedded sources" % config)
        if not own.embedsrcs or not all([file.basename == "data.txt" for file in own.embedsrcs]):
            errors.append("%s: missing embedded data: %s" % (config, _paths(own.embedsrcs)))
        expected_embedded = embedded.srcs if config in ["embedded", "both"] else []
        expected_own = (own_sources if config in ["own", "both"] else []) + expected_embedded
        for name, info, expected in [("own", own, expected_own), ("embedded", embedded, expected_embedded)]:
            actual = info.cover.to_list()
            if _paths(actual) != _paths(expected):
                errors.append("%s/%s: cover = %s, want %s" % (config, name, _paths(actual), _paths(expected)))
            if any([file in actual for file in info.embedsrcs]):
                errors.append("%s/%s: embedded data selected as Go coverage" % (config, name))
    return [AnalysisTestResultInfo(success = not errors, message = "\n".join(errors))]

coverage_selection_test = rule(
    implementation = _coverage_selection_test_impl,
    attrs = {
        "targets": attr.label_list(
            cfg = _coverage_configs,
            providers = [GoInfo],
            doc = "The generated-own and embedded library, in that order.",
            mandatory = True,
        ),
        "_allowlist_function_transition": attr.label(
            default = "@bazel_tools//tools/allowlists/function_transition_allowlist",
        ),
    },
    analysis_test = True,
    test = True,
)
