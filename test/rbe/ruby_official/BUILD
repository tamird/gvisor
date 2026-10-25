# Private dependency preparation for the official Ruby HTTP experiment.

load("@rules_python//python:defs.bzl", "py_test")
load("//tools:arch.bzl", "arch_genrule")

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

filegroup(
    name = "source_gems",
    srcs = [
        "@ruby_http_gem_base64//file",
        "@ruby_http_gem_logger//file",
        "@ruby_http_gem_mustermann//file",
        "@ruby_http_gem_nio4r//file",
        "@ruby_http_gem_puma//file",
        "@ruby_http_gem_rack//file",
        "@ruby_http_gem_rack_protection//file",
        "@ruby_http_gem_rack_session//file",
        "@ruby_http_gem_rackup//file",
        "@ruby_http_gem_sinatra//file",
        "@ruby_http_gem_tilt//file",
        "@ruby_http_gem_webrick//file",
    ],
)

genrule(
    name = "gems_amd64",
    srcs = [
        "build.rb",
        ":source_gems",
    ],
    outs = [
        "gems_amd64.json",
        "gems_amd64.tar",
    ],
    cmd = "/usr/local/bin/ruby $(location build.rb) amd64 $(location gems_amd64.tar) $(location gems_amd64.json) $(locations :source_gems)",
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
        "network": "off",
        "workload-isolation-type": "oci",
    },
    tags = ["manual"],
    visibility = ["//visibility:public"],
)

genrule(
    name = "gems_arm64",
    srcs = [
        "build.rb",
        ":source_gems",
    ],
    outs = [
        "gems_arm64.json",
        "gems_arm64.tar",
    ],
    cmd = "/usr/local/bin/ruby $(location build.rb) arm64 $(location gems_arm64.tar) $(location gems_arm64.json) $(locations :source_gems)",
    exec_compatible_with = [
        "@platforms//cpu:aarch64",
        "@platforms//os:linux",
    ],
    exec_properties = {
        "Arch": "arm64",
        "EstimatedFreeDiskBytes": "4GB",
        "EstimatedMemory": "2GB",
        "OSFamily": "linux",
        "container-image": "docker://mirror.gcr.io/library/ruby@sha256:9b49dd3f66ec99f1b7b45195e7624a6705cae6b29c4ed94c8028f723f6175a04",
        "network": "off",
        "workload-isolation-type": "oci",
    },
    tags = ["manual"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "gems_tar",
    srcs = select({
        "@platforms//cpu:aarch64": [":gems_arm64.tar"],
        "@platforms//cpu:x86_64": [":gems_amd64.tar"],
    }),
    visibility = ["//images/image-test/ruby:__pkg__"],
)

filegroup(
    name = "gems_profile",
    srcs = select({
        "@platforms//cpu:aarch64": [":gems_arm64.json"],
        "@platforms//cpu:x86_64": [":gems_amd64.json"],
    }),
)

arch_genrule(
    name = "qualification_gems",
    src = ":gems_tar",
    template = "qualification_gems_%s.tar",
)

arch_genrule(
    name = "qualification_profiles",
    src = ":gems_profile",
    template = "qualification_profile_%s.json",
)

py_test(
    name = "image_identity_test",
    size = "large",
    srcs = ["image_identity_test.py"],
    args = [
        "$(locations //images/image-test/ruby:qualification_ruby_archives)",
        "$(locations //images/image-test/ruby:qualification_ruby_contexts)",
        "$(locations :qualification_gems)",
        "$(locations :qualification_profiles)",
    ],
    data = glob(["inputs/*.json"]) + [
        ":qualification_gems",
        ":qualification_profiles",
        "//images/image-test/ruby:qualification_ruby_archives",
        "//images/image-test/ruby:qualification_ruby_contexts",
    ],
    tags = ["manual"],
    deps = ["//test/rbe/ruby_distro:image_archive"],
)
