package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

filegroup(
    name = "sources",
    srcs = glob(["**"]),
    visibility = ["//images:__pkg__"],
)

exports_files(
    [
        "build.sh",
        "checks.rb",
    ],
    visibility = ["//website:__pkg__"],
)
