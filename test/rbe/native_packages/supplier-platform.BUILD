# The root execution platform is private. Preserve its preset and properties,
# using BuildBuddy's name for the ordinary network mode.
platform(
    name = "buildbuddy",
    parents = ["@rbe_ubuntu2404//config:platform"],
    exec_properties = {
        "dockerNetwork": "bridge",
        "dockerPrivileged": "true",
        "Pool": "default",
    },
    visibility = ["//visibility:public"],
)
