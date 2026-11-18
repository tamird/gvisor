"""Production atomic pointer code-generation test inputs."""

load("@io_bazel_rules_go//go:def.bzl", "GoArchive", "GoInfo")
load("//tools:arch.bzl", "arch_transition_impl", "transition_allowlist")
load("//tools/bazeldefs:defs.bzl", "arch_config")
load("//tools/bazeldefs:go.bzl", "go_context", "go_rule")

def _optimized_arches(settings, attr):
    result = arch_transition_impl(settings, attr)
    for values in result.values():
        values.update({
            "//command_line_option:compilation_mode": "opt",
            "//command_line_option:strip": "never",
            "//command_line_option:stamp": False,
            "@io_bazel_rules_go//go/config:debug": False,
            "@io_bazel_rules_go//go/config:msan": False,
            "@io_bazel_rules_go//go/config:gc_goopts": [],
            "@io_bazel_rules_go//go/config:gc_linkopts": [],
        })
    return result

_optimized_arch_transition = transition(
    implementation = _optimized_arches,
    inputs = [],
    outputs = arch_config + [
        "//command_line_option:compilation_mode",
        "//command_line_option:strip",
        "//command_line_option:stamp",
        "@io_bazel_rules_go//go/config:debug",
        "@io_bazel_rules_go//go/config:msan",
        "@io_bazel_rules_go//go/config:gc_goopts",
        "@io_bazel_rules_go//go/config:gc_linkopts",
    ],
)

def _inputs_impl(ctx):
    go = go_context(ctx)
    links = {}
    records = []
    for arch in ["amd64", "arm64"]:
        sentry = ctx.split_attr.sentry[arch]
        kernel = ctx.split_attr.kernel[arch]
        kvm = ctx.split_attr.kvm[arch]
        negative = ctx.split_attr.negative[arch]
        for target in [sentry, kernel, kvm]:
            archive = target[GoArchive]
            if archive.mode.debug or archive.mode.race or archive.mode.msan or archive.source.gc_goopts:
                fail("expected ordinary optimized production archive: %s" % target.label)
        if sentry[GoArchive].mode.strip:
            fail("production DWARF must be retained")
        if negative[GoArchive].source.gc_goopts != ["-l"]:
            fail("negative control must disable inlining for the actual package")
        executable = sentry[DefaultInfo].files.to_list()
        if len(executable) != 1:
            fail("expected one production Sentry executable")
        artifacts = {
            "elf": executable[0],
            "kernel_archive": kernel[GoArchive].data.file,
            "kvm_archive": kvm[GoArchive].data.file,
            "negative_archive": negative[GoArchive].data.file,
        }
        prefix = ctx.label.package + "/" + ctx.label.name + "/" + arch + "/"
        artifact_paths = {}
        for name, file in artifacts.items():
            alias = prefix + name
            links[alias] = file
            artifact_paths[name] = alias
        sources = []
        for logical, target in [
            ("pkg/sentry/kernel/fd_table_unsafe.go", kernel),
            ("pkg/sentry/kernel/atomicptr_bucket_slice.go", kernel),
            ("pkg/sentry/kernel/atomicptr_bucket.go", kernel),
            ("pkg/sentry/kernel/atomicptr_descriptor.go", kernel),
            ("pkg/sentry/platform/kvm/seccomp_mmap_unsafe.go", kvm),
            ("pkg/sentry/platform/kvm/atomicptr_machine.go", kvm),
        ]:
            matches = [file for file in target[GoInfo].srcs if file.short_path == logical]
            if len(matches) != 1:
                fail("expected one production source %s, got %s" % (logical, matches))
            alias = prefix + "sources/" + logical
            links[alias] = matches[0]
            sources.append(alias)
        records.append(struct(
            arch = arch,
            artifacts = artifact_paths,
            sources = sources,
        ))
    manifest = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(manifest, json.encode_indent(struct(
        go = go.go.short_path,
        sdk = go.lang_version,
        records = records,
    )) + "\n")
    return [DefaultInfo(
        files = depset([manifest]),
        # Short paths are not unique across configurations of the same target.
        # Publish only these architecture-qualified aliases for split inputs.
        runfiles = ctx.runfiles(files = [manifest] + go.runfiles.to_list(), symlinks = links),
    )]

atomicptr_codegen_inputs = go_rule(
    rule,
    implementation = _inputs_impl,
    attrs = {
        "sentry": attr.label(default = "//runsc/cmd/sentry:gvisor_sentry", cfg = _optimized_arch_transition),
        "kernel": attr.label(default = "//pkg/sentry/kernel:kernel", cfg = _optimized_arch_transition, providers = [GoArchive, GoInfo]),
        "kvm": attr.label(default = "//pkg/sentry/platform/kvm:kvm", cfg = _optimized_arch_transition, providers = [GoArchive, GoInfo]),
        "negative": attr.label(default = "//pkg/sentry/platform/kvm:atomicptr_noinline", cfg = _optimized_arch_transition, providers = [GoArchive]),
        "_allowlist_function_transition": attr.label(default = transition_allowlist),
    },
)
