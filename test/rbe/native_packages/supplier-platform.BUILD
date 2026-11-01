# BuildBuddy calls the upstream standard network mode "bridge".
platform(
    name = "buildbuddy",
    parents = ["//:rbe_ubuntu2404_platform"],
    exec_properties = {"dockerNetwork": "bridge"},
    visibility = ["//visibility:public"],
)
