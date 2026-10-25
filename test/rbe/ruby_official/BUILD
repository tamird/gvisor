# Private dependency preparation for the official Ruby HTTP experiment.

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

genrule(
    name = "resolve",
    srcs = [
        "Gemfile",
        "resolve.rb",
    ],
    outs = ["resolved-gems.tar"],
    cmd = "/usr/local/bin/ruby $(location resolve.rb) $(location Gemfile) $@",
    exec_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    exec_properties = {
        "Arch": "amd64",
        "EstimatedFreeDiskBytes": "4GB",
        "EstimatedMemory": "2GB",
        "OSFamily": "linux",
        "container-image": "docker://mirror.gcr.io/library/ruby@sha256:b2a0f4f8d2c02122da0b252c272558b75a7a29943ca33597dcc5744cba62c305",
        "network": "external",
        "workload-isolation-type": "oci",
    },
    tags = ["manual"],
    visibility = ["//visibility:public"],
)
