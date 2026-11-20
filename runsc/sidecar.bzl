"""C programs shipped alongside runsc that run without a C runtime."""

load("//tools:defs.bzl", "freestanding_cc_binary")

def freestanding_sidecar(name, src, out, visibility = None):
    """Builds a static executable with its own process entry point.

    Args:
      name: Public Bazel target for the sidecar.
      src: C source file defining _start.
      out: Filename used by runsc and the sidecar tests.
      visibility: Visibility of the sidecar target.
    """
    freestanding_cc_binary(
        name = name,
        out = out,
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
        visibility = visibility,
    )
