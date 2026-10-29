"""C programs shipped alongside runsc that run without a C runtime."""

load("@with_cfg.bzl", "with_cfg")
load("//tools:defs.bzl", "cc_binary")

# --custom_malloc overrides cc_binary's malloc attribute, even under -nostdlib.
# These programs issue syscalls directly and have no allocator or sanitizer runtime.
_sidecar_binary, _ = with_cfg(cc_binary, executable = True).set(
    "custom_malloc",
    Label("@bazel_tools//tools/cpp:malloc"),  # Bazel's empty default allocator.
).extend("linkopt", ["-fno-sanitize=all"]).build()

def freestanding_sidecar(name, src, out):
    """Builds a static executable with its own process entry point.

    Args:
      name: Public Bazel target for the sidecar.
      src: C source file defining _start.
      out: Filename used by runsc and the sidecar tests.
    """
    _sidecar_binary(
        name = out,
        srcs = [src],
        copts = [
            "-Os",
            "-ffreestanding",
            "-fno-pie",
            "-fno-sanitize=all",
            "-fno-stack-protector",
            "-fno-asynchronous-unwind-tables",
            "-fno-unwind-tables",
        ],
        # These executables have their own entry point and use only syscalls.
        # Do not add either toolchain's hosted libraries or startup code.
        features = [
            "-coverage",
            "-default_link_flags",
            "-default_linker_libraries",
            "-heap_partitioning",
            "-pie",
            "-static_linking_mode",
            "-use_header_modules",
            "-use_pthread",
        ],
        linkopts = [
            "-static",
            "-nostdlib",
            "-nostartfiles",
            "-Wl,--build-id=none",
            "-Wl,--gc-sections",
            # Avoid padding loadable segments to a page boundary.
            "-Wl,-n",
            "-Wl,-s",
        ],
        linkstatic = True,
        stamp = 0,
        tcmalloc = False,
        visibility = ["//visibility:public"],
    )
    native.alias(
        name = name,
        actual = ":" + out,
        visibility = ["//visibility:public"],
    )
