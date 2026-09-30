package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# Include the complete contexts, including files owned by nested packages.
# tools/images.mk remains responsible for architecture selection and hashing.
filegroup(
    name = "sources",
    srcs = glob(["**"]) + [
        "//images/gpu/ollama/client:sources",
        "//images/jekyll:sources",
    ],
    visibility = ["//test/docker:__pkg__"],
)
