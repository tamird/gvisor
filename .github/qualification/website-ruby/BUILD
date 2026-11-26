load("@rules_ruby//ruby:defs.bzl", "rb_binary")
exports_files(["Gemfile", "Gemfile.lock"])
genrule(
    name = "native_tools",
    visibility = ["@website_bundle//:__pkg__"],
    outs = ["native-tools.txt"],
    cmd = "set -eu; { for tool in cc c++ make; do command -v $$tool; done; cc --version; make --version; uname -m; } >$@",
    exec_compatible_with = ["//tools/bazeldefs:rbe_ubuntu24_c"],
    tags = ["no-local"],
)
rb_binary(
    name = "preflight",
    srcs = ["preflight.rb", "html_checks.rb"],
    main = "preflight.rb",
    data = ["@nodejs//:node_bin"],
    deps = ["@website_bundle"],
)
