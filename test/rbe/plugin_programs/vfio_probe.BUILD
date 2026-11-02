# Qualification-only target, appended byte-for-byte by this carrier.
make(
    name = "vfio_command_probe",
    args = _MAKE_ARGS + [
        "-C",
        "dpdk",
        "-f",
        "Makefile",
        "-f",
        "\"$EXT_BUILD_ROOT/$(execpath @//test/rbe/plugin_programs:vfio_probe.mk)\"",
        "'VFIO_PROBE:=$(EXT_BUILD_ROOT)/$(execpath @//test/rbe/plugin_programs:vfio_api_probe.c)'",
    ],
    build_data = _BUILD_DATA + [
        "@//test/rbe/plugin_programs:vfio_probe.mk",
        "@//test/rbe/plugin_programs:vfio_api_probe.c",
    ],
    build_shell = "@plugin_programs//:bin/bash",
    env = _ENV,
    exec_compatible_with = _EXEC_CONSTRAINTS,
    exec_properties = _EXEC_PROPERTIES,
    lib_source = ":sources",
    out_data_files = [
        "pci-vfio-dry-run.txt",
        "vfio-api-command.txt",
    ],
    out_include_dir = "",
    postfix_script = """
mkdir -p $$INSTALLDIR
cp dpdk/_build/pci-vfio-dry-run.txt dpdk/_build/vfio-api-command.txt $$INSTALLDIR/
""",
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
        "@llvm//constraints/libc:musl",
    ],
    targets = ["vfio-command"],
    toolchains = ["@//external/tools/plugin-stack:toolchain"],
    visibility = ["//visibility:public"],
    deps = select({
        ":musl": ["@plugin_programs//:bsd_queue"],
        "//conditions:default": [],
    }),
)
