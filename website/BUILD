load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("@rules_img//img:load.bzl", "image_load")
load("//website:render.bzl", "render_site", "syscall_pages")
load("//images:defs.bzl", "docker_image")
load("//tools:defs.bzl", "bzl_library", "pkg_tar")
load("//tools/yamltest:defs.bzl", "yaml_test")
load("//website:defs.bzl", "doc", "docs")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

docker_image(
    name = "website",
    data = ":files",
    statements = [
        "EXPOSE 8080/tcp",
        'ENTRYPOINT ["/server"]',
    ],
    tags = [
        "local",
        "manual",
        "no-sandbox",
    ],
)

# files is the full file system of the generated container.
#
# It must collect the all tarballs (produced by the rules below), and run it
# through the Dockerfile to generate the site. Note that this checks all links,
# and therefore requires all static content to be present as well.
#
# Note that this rule violates most aspects of hermetic builds. However, this
# works much more reliably than depending on the container_image rules from
# bazel itself, which are convoluted and seem to have a hard time even finding
# the toolchain.
genrule(
    name = "files",
    srcs = [
        ":config",
        ":css",
        ":docs",
        ":static",
        ":syscallmd",
        "//website/blog:posts",
        "//website/cmd/server",
        "@google_root_pem//file",
    ],
    outs = ["files.tgz"],
    cmd = "set -x; " +
          "T=$$(mktemp -d); " +
          "mkdir -p $$T/input && " +
          "mkdir -p $$T/output/_site && " +
          "tar -xf $(location :config)    -C $$T/input && " +
          "tar -xf $(location :css)       -C $$T/input && " +
          "tar -xf $(location :docs)      -C $$T/input && " +
          "tar -xf $(location :syscallmd) -C $$T/input && " +
          "tar -xf $(location //website/blog:posts) -C $$T/input && " +
          "find $$T/input -type f -exec chmod u+rw {} \\; && " +
          "docker run -i --user $$(id -u):$$(id -g) " +
          "-v $$(readlink -m $$T/input):/input " +
          "-v $$(readlink -m $$T/output/_site):/output " +
          "gvisor.dev/images/jekyll && " +
          "tar -xf $(location :static) -C $$T/output/_site && " +
          "docker run -i --user $$(id -u):$$(id -g) " +
          "-v $$(readlink -m $$T/output/_site):/output " +
          "gvisor.dev/images/jekyll " +
          "ruby /checks.rb " +
          "/output && " +
          "cp $(location //website/cmd/server) $$T/output/server && " +
          "mkdir -p $$T/output/etc/ssl && " +
          "cp $(location @google_root_pem//file) $$T/output/etc/ssl/cert.pem && " +
          "tar -zcf $@ -C $$T/output . && " +
          "rm -rf $$T",
    tags = [
        "local",
        "manual",
        "no-sandbox",
    ],
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
        "//g3doc:release",
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
        "//g3doc/user_guide:no_root_container",
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
    cmd = "$(location //runsc/cmd/sentry:gvisor_sentry) -- help syscalls -format json -filename $@",
    tools = ["//runsc/cmd/sentry:gvisor_sentry"],
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

syscall_pages(name = "native_syscall_pages")

render_site(
    name = "rendered_site",
    exec_properties = {
        "OSFamily": "linux",
        "Arch": "amd64",
        "workload-isolation-type": "oci",
        "container-image": "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef",
    },
    srcs = [
        "_config.yml",
        "//website/application_compatibility:index.html",
        "//website/blog:index.html",
        "//website/security_track_record:index.html",
    ] + glob(["_data/**", "assets/**", "css/**", "_includes/**", "_layouts/**", "_plugins/**", "_sass/**"]),
    docs = [":docs", "//website/blog:posts"],
    static = ["archive.key"] + glob(["performance/**"]),
    css = ":css",
    syscalls = ":native_syscall_pages",
    driver = "render.rb",
    as_of = "2026-10-30T12:00:00Z",
    checks = "//test/rbe/website-jekyll:checks.rb",
    runtime = ["//test/rbe/website-jekyll:sdk_files"],
    gems = ["//test/rbe/website-jekyll:pinned_gems"],
    loader = "//test/rbe/website-jekyll:tool_files/lib/ld-musl-x86_64.so.1",
    tags = ["manual"],
)

image_layer(
    name = "site_layer",
    srcs = {
        "/_site": ":rendered_site",
        "/server": "//website/cmd/server",
        "/etc/ssl/cert.pem": "@google_root_pem//file",
    },
    file_metadata = {"/server": file_metadata(mode = "0755")},
    include_runfiles = False,
)

image_manifest(
    name = "site_image",
    layers = [":site_layer"],
    entrypoint = ["/server"],
    config_fragment = "server_config.json",
    working_dir = "/",
    tags = ["manual"],
)

image_load(name = "site_load", image = ":site_image", tag = "gvisor-website:qualify")
filegroup(name = "site_docker_archive", srcs = [":site_load"], output_group = "tarball")
