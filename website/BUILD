load("@bazel_skylib//rules:common_settings.bzl", "string_flag")
load("@rules_img//img:convert.bzl", "image_flatten")
load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("@rules_img//img:load.bzl", "image_load")
load("@tar.bzl//tar:tar.bzl", "tar")
load("//tools:defs.bzl", "bzl_library")
load("//tools/yamltest:defs.bzl", "yaml_test")
load("//website:defs.bzl", "doc", "docs")
load("//website:jekyll.bzl", "jekyll_sdk", "render_site", "syscall_pages")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# Sources are rendered and checked before being added to the serving image.
image_layer(
    name = "files",
    srcs = {
        "/_site": ":site",
        "/etc/ssl/cert.pem": "@google_root_pem//file",
        "/server": "//website/cmd/server",
    },
    compress = "gzip",
    file_metadata = {"/server": file_metadata(mode = "0755")},
    include_runfiles = False,
)

image_manifest(
    name = "site_image",
    config_fragment = "server_config.json",
    entrypoint = ["/server"],
    layers = [":files"],
    working_dir = "/",
)

image_load(
    name = "website",
    build_settings = {"website_image": ":image_name"},
    image = ":site_image",
    tag = "{{.website_image}}",
    tags = ["manual"],
)

string_flag(
    name = "image_name",
    build_setting_default = "gcr.io/gvisordev/gvisordev",
)

# The publisher supplies its timestamp explicitly. An unconfigured preview
# retains the date at which Bazel last built it.
string_flag(
    name = "publication_time",
    build_setting_default = "",
)

# Preserve the public css.tar and avoid recording the build user's identity or clock.
tar(
    name = "css",
    srcs = ["css/main.scss"],
    out = "css.tar",
    compress = "gzip",
    mtree = [
        ". type=dir uid=0 gid=0 mode=0700 time=0",
        "./css type=dir uid=0 gid=0 mode=0755 time=0",
        "./css/main.scss type=file uid=0 gid=0 mode=0644 time=0 content=$(execpath css/main.scss)",
    ],
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

syscall_pages(name = "syscallmd")

image_flatten(
    name = "jekyll_filesystem",
    image = "@jekyll_runtime",
)

jekyll_sdk(
    name = "jekyll_sdk",
    image = ":jekyll_filesystem",
)

render_site(
    name = "site",
    srcs = [
        "_config.yml",
        "//website/application_compatibility:index.html",
        "//website/blog:index.html",
        "//website/security_track_record:index.html",
    ] + glob([
        "_data/**",
        "_includes/**",
        "_layouts/**",
        "_plugins/**",
        "_sass/**",
        "assets/**",
        "css/**",
    ]),
    as_of = ":publication_time",
    checks = "html_checks.rb",
    css = ":css",
    docs = [
        ":docs",
        "//website/blog:posts",
    ],
    driver = "render.rb",
    exec_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    gems = [
        "@jekyll_gem_autoprefixer_rails//file",
        "@jekyll_gem_jekyll_autoprefixer//file",
        "@jekyll_gem_jekyll_feed//file",
        "@jekyll_gem_jekyll_relative_links//file",
        "@jekyll_gem_jekyll_sitemap//file",
    ],
    runtime = ":jekyll_sdk",
    static = ["archive.key"] + glob(["performance/**"]),
    syscalls = ":syscallmd",
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
