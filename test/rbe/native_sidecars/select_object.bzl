"""Request one existing LLVM Support object in the execution configuration."""

def _support_compile_impl(ctx):
    objects = [
        f
        for f in ctx.attr.support[OutputGroupInfo].compilation_outputs.to_list()
        if f.basename in ("PrettyStackTrace.o", "PrettyStackTrace.pic.o")
    ]
    if not objects:
        fail("Could not find the LLVM Support object for PrettyStackTrace.cpp")
    return [
        DefaultInfo(files = depset(objects)),
        OutputGroupInfo(toolchain_map = ctx.attr.header_map[DefaultInfo].files),
    ]

support_compile = rule(
    implementation = _support_compile_impl,
    attrs = {
        "header_map": attr.label(
            default = "@@llvm++llvm_toolchain_minimal+llvm-toolchain-minimal-linux-amd64//:module_map",
            cfg = "exec",
        ),
        "support": attr.label(default = "@llvm-project//llvm:Support", cfg = "exec"),
    },
)
