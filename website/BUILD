load("@rules_oci//oci:defs.bzl", "oci_image")
load("//images:defs.bzl", "docker_image")
load("//tools:arch.bzl", "select_arch")
load("//tools:defs.bzl", "bzl_library", "pkg_tar")
load("//tools/yamltest:defs.bzl", "yaml_test")
load("//website:defs.bzl", "doc", "docs", "website_files")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# Both the installed import adapter and the remote artifact use this metadata.
_WEBSITE_ENTRYPOINT = ["/server"]

_WEBSITE_PORTS = ["8080/tcp"]

docker_image(
    name = "website",
    data = ":files",
    statements = [
        "EXPOSE " + " ".join(_WEBSITE_PORTS),
        "ENTRYPOINT " + json.encode(_WEBSITE_ENTRYPOINT),
    ],
    tags = [
        "local",
        "manual",
        "no-sandbox",
    ],
)

# The same scratch filesystem used by docker import, packaged without a daemon.
oci_image(
    name = "image",
    architecture = select_arch(
        amd64 = "amd64",
        arm64 = "arm64",
    ),
    entrypoint = _WEBSITE_ENTRYPOINT,
    exposed_ports = _WEBSITE_PORTS,
    os = "linux",
    tags = ["manual"],
    target_compatible_with = ["@platforms//os:linux"],
    tars = [":files"],
    workdir = "/",
)

website_files(
    name = "files",
    archives = [
        ":config",
        ":css",
        ":docs",
        ":syscallmd",
        "//website/blog:posts",
    ],
    ca_certificate = "@google_root_pem//file",
    server = "//website/cmd/server",
    static = ":static",
    tags = ["manual"],
)

# static are the purely static parts of the site. These are effectively copied
# in after jekyll generates all the dynamic content.
pkg_tar(
    name = "static",
    srcs = [
        "archive.key",
    ] + glob([
        "performance/**",
    ]),
    strip_prefix = "./",
)

# main.scss requires front-matter to be processed.
genrule(
    name = "css",
    srcs = glob([
        "css/**",
    ]),
    outs = [
        "css.tar",
    ],
    cmd = "T=$$(mktemp -d); " +
          "mkdir -p $$T/css && " +
          "for file in $(SRCS); do " +
          "echo -en '---\\n---\\n' > $$T/css/$$(basename $$file) && " +
          "cat $$file >> $$T/css/$$(basename $$file); " +
          "done && " +
          "tar -C $$T -czf $@ . && " +
          "rm -rf $$T",
)

# config is "mostly" static content. These are parts of the site that are
# present when jekyll runs, but are not dynamically generated.
pkg_tar(
    name = "config",
    srcs = [
        "_config.yml",
        ":css",
        "//website/application_compatibility:index.html",
        "//website/blog:index.html",
        "//website/security_track_record:index.html",
    ] + glob([
        "_data/**",
        "assets/**",
        "_includes/**",
        "_layouts/**",
        "_plugins/**",
        "_sass/**",
    ]),
    strip_prefix = "./",
)

# index is the index file.
doc(
    name = "index",
    src = "index.md",
    layout = "base",
    permalink = "/",
)

# docs is the dynamic content of the site.
docs(
    name = "docs",
    deps = [
        ":index",
        "//:adopters",
        "//:code_of_conduct",
        "//:contributing",
        "//:governance",
        "//:security",
        "//g3doc:community",
        "//g3doc:index",
        "//g3doc:roadmap",
        "//g3doc:style",
        "//g3doc/architecture_guide:intro_to_gvisor",
        "//g3doc/architecture_guide:networking",
        "//g3doc/architecture_guide:performance",
        "//g3doc/architecture_guide:platforms",
        "//g3doc/architecture_guide:resources",
        "//g3doc/architecture_guide:security",
        "//g3doc/proposals:gsoc_2021",
        "//g3doc/user_guide:FAQ",
        "//g3doc/user_guide:checkpoint_restore",
        "//g3doc/user_guide:compatibility",
        "//g3doc/user_guide:debugging",
        "//g3doc/user_guide:filesystem",
        "//g3doc/user_guide:fs_snapshot",
        "//g3doc/user_guide:fuse",
        "//g3doc/user_guide:gpu",
        "//g3doc/user_guide:install",
        "//g3doc/user_guide:networking",
        "//g3doc/user_guide:observability",
        "//g3doc/user_guide:platforms",
        "//g3doc/user_guide:production",
        "//g3doc/user_guide:rdma",
        "//g3doc/user_guide:rootfs_snapshot",
        "//g3doc/user_guide:rootless",
        "//g3doc/user_guide:runtime_monitoring",
        "//g3doc/user_guide:systemd",
        "//g3doc/user_guide:tpu",
        "//g3doc/user_guide/containerd:configuration",
        "//g3doc/user_guide/containerd:containerd_11",
        "//g3doc/user_guide/containerd:crio",
        "//g3doc/user_guide/containerd:quick_start",
        "//g3doc/user_guide/personalities:bwrap",
        "//g3doc/user_guide/quick_start:docker",
        "//g3doc/user_guide/quick_start:kubernetes",
        "//g3doc/user_guide/quick_start:oci",
        "//g3doc/user_guide/sdk:go",
        "//g3doc/user_guide/sdk:python",
        "//g3doc/user_guide/sdk:python_quickstart",
        "//g3doc/user_guide/sdk:quickstart",
        "//g3doc/user_guide/tutorials:cni",
        "//g3doc/user_guide/tutorials:docker",
        "//g3doc/user_guide/tutorials:docker_compose",
        "//g3doc/user_guide/tutorials:docker_in_gke_sandbox",
        "//g3doc/user_guide/tutorials:docker_in_gvisor",
        "//g3doc/user_guide/tutorials:falco",
        "//g3doc/user_guide/tutorials:knative",
        "//g3doc/user_guide/tutorials:kubernetes",
    ],
)

# Generate JSON for system call tables
genrule(
    name = "syscalljson",
    outs = ["syscalls.json"],
    cmd = "$(location //runsc) -- help syscalls -format json -filename $@",
    tools = ["//runsc"],
)

# Generate markdown from the json dump.
genrule(
    name = "syscallmd",
    srcs = [":syscalljson"],
    outs = ["syscallsmd"],
    cmd = "T=$$(mktemp -d) && " +
          "$(location //website/cmd/syscalldocs) -in $< -out $$T && " +
          "tar -C $$T -czf $@ . && " +
          "rm -rf $$T",
    tools = ["//website/cmd/syscalldocs"],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = ["//visibility:private"],
    deps = [
        "//tools:defs_bzl",
        "@bazel_skylib//lib:shell",
    ],
)

yaml_test(
    name = "cve_track_record_test",
    srcs = ["_data/cve-track-record.yml"],
    disallow_comments = True,
    schema = "//website:cve-track-record.schema.json",
)

yaml_test(
    name = "application_compatibility_test",
    srcs = ["_data/application-compatibility.yml"],
    schema = "//website:application-compatibility.schema.json",
)
