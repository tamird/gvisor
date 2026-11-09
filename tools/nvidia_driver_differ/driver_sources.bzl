"""Source archives for the NVIDIA driver differ and ABI test."""

load("@bazel_tools//tools/build_defs/repo:http.bzl", "http_archive")
load(":driver_pins.bzl", "DRIVER_PINS")

def _repo_name(version):
    return "nvidia_driver_" + version.replace(".", "_")

_DRIVER_SOURCES = {
    version: "@" + _repo_name(version) if pin else None
    for version, pin in DRIVER_PINS.items()
}

# Both programs start in their repository's runfiles directory. COPYING
# marks the root of each archive; the parser also needs its header tree.
DRIVER_SOURCE_DATA = [
    repo + label
    for repo in _DRIVER_SOURCES.values()
    if repo
    for label in ["//:COPYING", "//:sources"]
]

DRIVER_SOURCE_ENV = {
    "NVIDIA_DRIVER_SOURCES": json.encode({
        version: "$(rootpath %s//:COPYING)" % repo if repo else None
        for version, repo in _DRIVER_SOURCES.items()
    }),
}

def _driver_sources_impl(ctx):
    for version, pin in DRIVER_PINS.items():
        if pin:
            commit, sha256 = pin
            http_archive(
                name = _repo_name(version),
                build_file = Label("//tools/nvidia_driver_differ:driver_source.BUILD"),
                sha256 = sha256,
                strip_prefix = "open-gpu-kernel-modules-" + commit,
                urls = ["https://github.com/NVIDIA/open-gpu-kernel-modules/archive/" + commit + ".tar.gz"],
            )
    return ctx.extension_metadata(
        root_module_direct_deps = [],
        root_module_direct_dev_deps = "all",
        reproducible = True,
    )

driver_sources = module_extension(implementation = _driver_sources_impl)
