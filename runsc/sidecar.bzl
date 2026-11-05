"""C programs shipped alongside runsc that run without a C runtime."""

load("//tools:defs.bzl", "freestanding_executable", "linux_headers")

def freestanding_sidecar(name, src, out):
    """Builds a static executable with its own process entry point.

    Args:
      name: Public Bazel target for the sidecar.
      src: C source file defining _start.
      out: Filename used by runsc and the sidecar tests.
    """
    freestanding_executable(
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
        deps = [linux_headers],
        # These executables have their own entry point and use only syscalls.
        # Do not add either toolchain's hosted libraries or startup code.
        features = [
            "-coverage",
            "-default_link_flags",
            "-default_linker_libraries",
            "-heap_partitioning",
            "-pie",
            "-static_link_cpp_runtimes",
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
        visibility = ["//visibility:public"],
    )
