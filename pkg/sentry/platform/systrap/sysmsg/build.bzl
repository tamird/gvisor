"""Builds the Sysmsg stub with the selected C toolchain."""

load("@rules_cc//cc:find_cc_toolchain.bzl", "CC_TOOLCHAIN_TYPE", "find_cc_toolchain", "use_cc_toolchain")
load("//tools:arch.bzl", "select_arch")
load("//tools:defs.bzl", "BuildSettingInfo")

def _execution_requirements(ctx):
    if ctx.attr._native_c[BuildSettingInfo].value:
        # The native toolchain requires the pinned image execution platform.
        return {"no-local": "1"}
    return {"no-remote": "1"}

def _attrs(extra):
    return dict({
        "_native_c": attr.label(default = "//tools/bazeldefs:rbe_native_c", providers = [BuildSettingInfo]),
    }, **extra)

def _compile_impl(ctx):
    cc = find_cc_toolchain(ctx)
    args = ctx.actions.args()
    args.add_all(ctx.attr.copts)
    args.add("-c")
    args.add_all([src for src in ctx.files.srcs if not src.basename.endswith(".h")])
    args.add("-o")
    args.add(ctx.outputs.out)
    ctx.actions.run(
        executable = cc.compiler_executable,
        arguments = [args],
        inputs = depset(ctx.files.srcs, transitive = [cc.all_files]),
        outputs = [ctx.outputs.out],
        mnemonic = "SysmsgCompile",
        execution_requirements = _execution_requirements(ctx),
        use_default_shell_env = True,
        toolchain = CC_TOOLCHAIN_TYPE,
    )
    return [DefaultInfo(files = depset([ctx.outputs.out]))]

_compile = rule(
    implementation = _compile_impl,
    attrs = _attrs({
        "srcs": attr.label_list(allow_files = True),
        "out": attr.output(mandatory = True),
        "copts": attr.string_list(),
    }),
    fragments = ["cpp"],
    toolchains = use_cc_toolchain(),
)

def cc_pie_obj(name, srcs, outs):
    """Compiles one freestanding position-independent Sysmsg object.

    Args:
        name: Target name.
        srcs: Source and header files.
        outs: One object output.
    """
    if len(outs) != 1:
        fail("cc_pie_obj requires exactly one output")
    _compile(
        name = name,
        srcs = srcs,
        out = outs[0],
        copts = select({
            "//tools/bazeldefs:pagesize_64k": ["-DPAGE_SIZE=65536"],
            "//conditions:default": ["-DPAGE_SIZE=4096"],
        }) + [
            "-Wall",
            "-Werror",
            "-Wno-unused-command-line-argument",
            "-fpie",
        ] + select_arch(
            amd64 = ["-O2"],
            # At -O0 clang may emit memcpy for structure copies, which the
            # freestanding stub cannot resolve.
            arm64 = ["-O1", "-mno-outline-atomics"],
        ) + [
            "-fno-builtin",
            "-ffreestanding",
            "-mgeneral-regs-only",
            # Debug info contains volatile absolute paths.
            "-g0",
            "-Wa,--noexecstack",
            "-fno-asynchronous-unwind-tables",
            "-fno-stack-protector",
        ],
    )

def _link_impl(ctx):
    cc = find_cc_toolchain(ctx)
    args = ctx.actions.args()
    args.add_all(["-pie", "-z", "noexecstack", "-T"])
    args.add(ctx.file.linker_script)
    args.add_all(ctx.files.ordered_objects)
    args.add("-o")
    args.add(ctx.outputs.out)
    ctx.actions.run(
        executable = cc.ld_executable,
        arguments = [args],
        inputs = depset(ctx.files.ordered_objects + [ctx.file.linker_script], transitive = [cc.all_files]),
        outputs = [ctx.outputs.out],
        mnemonic = "SysmsgLink",
        execution_requirements = _execution_requirements(ctx),
        use_default_shell_env = True,
        toolchain = CC_TOOLCHAIN_TYPE,
    )
    return [DefaultInfo(files = depset([ctx.outputs.out]))]

sighandler_object = rule(
    implementation = _link_impl,
    attrs = _attrs({
        "ordered_objects": attr.label_list(allow_files = True),
        "linker_script": attr.label(allow_single_file = True, mandatory = True),
        "out": attr.output(mandatory = True),
    }),
    fragments = ["cpp"],
    toolchains = use_cc_toolchain(),
)

def _binary_impl(ctx):
    cc = find_cc_toolchain(ctx)
    ctx.actions.run(
        executable = cc.objcopy_executable,
        arguments = ["-O", "binary", ctx.file.src.path, ctx.outputs.out.path],
        inputs = depset([ctx.file.src], transitive = [cc.all_files]),
        outputs = [ctx.outputs.out],
        mnemonic = "SysmsgObjcopy",
        execution_requirements = _execution_requirements(ctx),
        use_default_shell_env = True,
        toolchain = CC_TOOLCHAIN_TYPE,
    )
    return [DefaultInfo(files = depset([ctx.outputs.out]))]

sighandler_binary = rule(
    implementation = _binary_impl,
    attrs = _attrs({
        "src": attr.label(allow_single_file = True, mandatory = True),
        "out": attr.output(mandatory = True),
    }),
    fragments = ["cpp"],
    toolchains = use_cc_toolchain(),
)

def _offsets_impl(ctx):
    cc = find_cc_toolchain(ctx)
    ctx.actions.run_shell(
        command = 'bash -x "$1" sighandler Sighandler "$2" "$3" > "$4"',
        arguments = [ctx.file._script.path, ctx.file.src.path, cc.nm_executable, ctx.outputs.out.path],
        inputs = depset([ctx.file.src, ctx.file._script], transitive = [cc.all_files]),
        outputs = [ctx.outputs.out],
        mnemonic = "SysmsgOffsets",
        execution_requirements = _execution_requirements(ctx),
        use_default_shell_env = True,
        toolchain = CC_TOOLCHAIN_TYPE,
    )
    return [DefaultInfo(files = depset([ctx.outputs.out]))]

sighandler_go = rule(
    implementation = _offsets_impl,
    attrs = _attrs({
        "src": attr.label(allow_single_file = True, mandatory = True),
        "out": attr.output(mandatory = True),
        "_script": attr.label(default = Label("//pkg/sentry/platform/systrap/sysmsg:gen_offsets_go.sh"), allow_single_file = True),
    }),
    fragments = ["cpp"],
    toolchains = use_cc_toolchain(),
)
