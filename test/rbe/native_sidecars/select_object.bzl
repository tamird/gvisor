SupportFiles = provider(fields = ["objects", "maps"])

def _files(target, ctx):
    if target.label == Label("@llvm-project//llvm:Support"):
        objects = [f for f in target[OutputGroupInfo].compilation_outputs.to_list() if f.basename == "PrettyStackTrace.pic.o"]
        if len(objects) != 1:
            fail("expected one PrettyStackTrace.pic.o: %s" % objects)
        return [SupportFiles(objects = depset(objects), maps = ctx.attr._map[DefaultInfo].files)]
    children = []
    for name in ["dep", "deps"]:
        val = getattr(ctx.rule.attr, name, [])
        if type(val) == "Target":
            val = [val]
        children.extend([v[SupportFiles] for v in val if type(v) == "Target" and SupportFiles in v])
    return [SupportFiles(objects = depset(transitive = [x.objects for x in children]), maps = depset(transitive = [x.maps for x in children]))]

_support = aspect(
    implementation = _files,
    attr_aspects = ["dep", "deps"],
    attrs = {"_map": attr.label(default = "@@llvm++llvm_toolchain_minimal+llvm-toolchain-minimal-linux-amd64//:module_map")},
)

def _static(settings, attr):
    return {
        "@llvm//runtimes:linkmode": "static",
        "//command_line_option:compilation_mode": "dbg",
        "//command_line_option:features": settings["//command_line_option:features"] + ["-no_exceptions", "-no_rtti", "-omit_frame_pointer"],
    }

_runtime = transition(
    implementation = _static,
    inputs = ["//command_line_option:features"],
    outputs = ["@llvm//runtimes:linkmode", "//command_line_option:compilation_mode", "//command_line_option:features"],
)

def _one(ctx):
    files = ctx.attr.symbolizer[0][SupportFiles]
    if len(files.objects.to_list()) != 1:
        fail("expected one Support object: %s" % files.objects)
    return [DefaultInfo(files = files.objects), OutputGroupInfo(physical_map = files.maps)]

probe = rule(
    implementation = _one,
    attrs = {
        "symbolizer": attr.label(default = "@llvm-project//compiler-rt:sanitizer_common_symbolizer_internal_unsanitized", cfg = _runtime, aspects = [_support]),
        "_allowlist_function_transition": attr.label(default = "@bazel_tools//tools/allowlists/function_transition_allowlist"),
    },
)
