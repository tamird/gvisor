load(":nodes.bzl", "node")
load("//tools:deps.bzl", "presubmit_deps_test")

package(default_visibility = ["//visibility:public"])

constraint_setting(name = "fixture_abi")
constraint_value(name = "fixture_abi_set", constraint_setting = ":fixture_abi")
platform(name = "custom_platform", parents = ["//tools/bazeldefs:linux_amd64"], constraint_values = [":fixture_abi_set"])
config_setting(name = "custom_abi", constraint_values = [":fixture_abi_set"])
config_setting(name = "race", flag_values = {"@io_bazel_rules_go//go/config:race": "true"})

[node(name = n) for n in ["allowed_amd", "allowed_arm", "denied_amd", "denied_arm", "race_on", "race_off", "custom", "ordinary"]]
node(name = "amd_ok", deps = [":allowed_amd"])
node(name = "amd_bad", deps = [":denied_amd"])
node(name = "arm_ok", deps = [":allowed_arm"])
node(name = "arm_bad", deps = [":denied_arm"])
node(name = "race_root", deps = select({":race": [":race_on"], "//conditions:default": [":race_off"]}))
node(name = "custom_root", deps = select({":custom_abi": [":custom"], "//conditions:default": [":ordinary"]}))

[
    presubmit_deps_test(
        name = name,
        targets = select({"//tools/bazeldefs:arm64": [arm], "//conditions:default": [amd]}),
        allowed = select({"//tools/bazeldefs:arm64": [":allowed_arm"], "//conditions:default": [":allowed_amd"]}),
    )
    for name, amd, arm in [
        ("selected_policy_test", ":amd_ok", ":arm_ok"),
        ("reject_amd_test", ":amd_bad", ":arm_ok"),
        ("reject_arm_test", ":amd_ok", ":arm_bad"),
    ]
]

presubmit_deps_test(name = "race_policy_test", targets = [":race_root"], allowed = [":race_on"])
presubmit_deps_test(name = "custom_policy_test", targets = [":custom_root"], allowed = [":custom"])
