"""Build the preload library for the intercepted program's libc ABI."""

load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")
load("//tools:defs.bzl", "cc_binary", "select_arch")

# run_sniffer embeds this library but does not load it itself. Keep the library
# compatible with GNU applications such as nvidia-smi even when the embedding
# static Go binary and its other dependencies are built against musl.
gnu_cc_binary, _gnu_cc_binary_transition = with_cfg(cc_binary).set(
    "platforms",
    select_arch(
        amd64 = [Label("@llvm//platforms:linux_x86_64")],
        arm64 = [Label("@llvm//platforms:linux_aarch64")],
    ),
).build()
