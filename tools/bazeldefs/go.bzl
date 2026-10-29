"""Go rules."""

load("@bazel_gazelle//:def.bzl", _gazelle = "gazelle")
load("@io_bazel_rules_go//go:def.bzl", "GoArchive", "GoInfo", _go_binary = "go_binary", _go_context = "go_context", _go_library = "go_library", _go_path = "go_path", _go_reset_target = "go_reset_target", _go_rule = "go_rule", _go_test = "go_test")
load("@io_bazel_rules_go//proto:def.bzl", _go_grpc_library = "go_grpc_library", _go_proto_library = "go_proto_library")
load("//tools/bazeldefs:cgroup_test.bzl", "cgroup_v1_tags", "cgroup_v1_variant", "cgroup_v2_variant")
load("//tools/bazeldefs:defs.bzl", "select_arch", "select_system")
load("//tools/bazeldefs:go_variants.bzl", "go_amd64_test", "go_arm64_test", "go_cgroup_v1_test", "static_go_amd64_test", "static_go_arm64_test", _go_cov = "go_cov", _static_go_binary = "go_binary", _static_go_cov = "static_go_cov", _static_go_test = "go_test")
load("//tools/bazeldefs:test_architectures.bzl", "test_architecture_tags", "test_architecture_variants")

gazelle = _gazelle

go_path = _go_path
go_reset_target = _go_reset_target
cov_available = True

# Runtime data binaries inherit their parent's musl platform, but rules_go
# resets the parent's static mode across data edges. Give these binaries their
# own static attribute so that build tools can still reset to their usual mode.
_DEFAULT_STATIC = select({
    Label("@llvm//constraints/libc:musl"): "on",
    "//conditions:default": "auto",
})

def _codeql_source(name, testonly, srcs = None, excluded_modes = None):
    tags = ["manual", "codeql-go-owner:" + str(Label("//%s:%s" % (native.package_name(), name)))]
    if excluded_modes:
        # Instrumented/race compilations are not the default source analysis.
        # Retain their original inputs for parity without analyzing that graph.
        if not srcs:
            fail("Configured Go analysis disposition requires original sources: " + name)
        native.filegroup(
            name = name + "_codeql",
            srcs = srcs,
            testonly = testonly,
            tags = tags + ["codeql-go-excluded:" + mode for mode in excluded_modes],
            visibility = ["//tools/codeql:__pkg__"],
        )
        return

    # An alias in the same package preserves the owner's visibility (including
    # package defaults) and configuration. It adds no compilation or runfiles.
    native.alias(
        name = name + "_codeql",
        actual = ":" + name,
        testonly = testonly,
        tags = tags,
        visibility = ["//tools/codeql:__pkg__"],
    )

def _go_proto_or_grpc_library(go_library_func, name, **kwargs):
    if "importpath" in kwargs:
        # If importpath is explicit, pass straight through.
        go_library_func(name = name, **kwargs)
        _codeql_source(name, kwargs.get("testonly"))
        return
    deps = []
    com_google_protobuf = "@com_google_protobuf//"
    org_golang_google_protobuf = "@org_golang_google_protobuf//"
    for d in (kwargs.pop("deps", []) or []):
        # Special cases: these protos have their Go definitions in a different
        # repository.
        if d == com_google_protobuf + ":timestamp_proto":
            deps.append(org_golang_google_protobuf + "types/known/timestamppb")
            continue
        if d == com_google_protobuf + ":any_proto":
            deps.append(org_golang_google_protobuf + "types/known/anypb")
            continue

        if "//" in d:
            repo, path = d.split("//", 1)
            deps.append(repo + "//" + path.replace("_proto", "_go_proto"))
        else:
            deps.append(d.replace("_proto", "_go_proto"))
    go_library_func(
        name = name + "_go_proto",
        importpath = "gvisor.dev/gvisor/" + native.package_name() + "/" + name + "_go_proto",
        proto = ":" + name + "_proto",
        deps = deps,
        **kwargs
    )
    _codeql_source(name + "_go_proto", kwargs.get("testonly"))

def go_proto_library(name, **kwargs):
    _go_proto_or_grpc_library(_go_proto_library, name, **kwargs)

def go_grpc_and_proto_libraries(name, **kwargs):
    _go_proto_or_grpc_library(_go_grpc_library, name, **kwargs)

def go_binary(name, static = False, pure = False, x_defs = None, **kwargs):
    """Build a go binary.

    Args:
        name: name of the target.
        static: build a static binary.
        pure: build without cgo.
        x_defs: additional definitions.
        **kwargs: rest of the arguments are passed to _go_binary.
    """
    _go_binary_with_rule(
        _static_go_binary if static else _go_binary,
        name = name,
        static = static,
        pure = pure,
        x_defs = x_defs,
        **kwargs
    )

def go_cov(name, static = False, **kwargs):
    """Build a coverage binary with the same options as go_binary."""
    _go_binary_with_rule(
        _static_go_cov if static else _go_cov,
        name = name,
        static = static,
        coverage = True,
        **kwargs
    )

def _go_binary_with_rule(binary_rule, name, static = False, pure = False, x_defs = None, coverage = False, **kwargs):
    if "noasan" in kwargs:
        # no-op option, will need to do transitions when sanitizer configs are defined.
        kwargs.pop("noasan")

    kwargs["static"] = "on" if static else _DEFAULT_STATIC
    if pure:
        kwargs["pure"] = "on"
    gc_goopts = select({
        "//conditions:default": kwargs.pop("gc_goopts", []),
        "//tools:debug": kwargs.pop("gc_goopts", []) + ["-N", "-l"],
    })
    base_gotags = kwargs.pop("gotags", [])
    kwargs["gotags"] = select({
        "//tools/bazeldefs:pagesize_64k": base_gotags + ["pagesize_64k"],
        "//tools:debug": base_gotags + ["debug"],
        "//conditions:default": base_gotags,
    }) + select({
        "//tools:lockdep": ["lockdep"],
        "//conditions:default": [],
    })
    binary_rule(
        name = name,
        x_defs = x_defs,
        gc_goopts = gc_goopts,
        **kwargs
    )
    excluded_modes = ["coverage"] if coverage else []
    race = kwargs.get("race", "auto")
    if race not in ["auto", "on", "off"]:
        fail("Go analysis requires an explicit race-mode disposition: " + name)
    if race == "on":
        excluded_modes.append("race")
    _codeql_source(name, kwargs.get("testonly"), kwargs.get("srcs"), excluded_modes)

def go_package(target):
    """Returns configured Go sources and canonical import identities."""

    # Binary rules expose the same source metadata through their archive.
    info = target[GoInfo] if go_has_source(target) else target[GoArchive].data
    return struct(
        srcs = list(info.srcs),
        archive = target[GoArchive].data.file,
        importpath = info.importmap,
        importpaths = [info.importpath] + list(info.importpath_aliases),
    )

def go_has_archive(target):
    """Returns whether the target exposes a compiled Go archive."""
    return GoArchive in target

def go_source_files(target):
    """Returns the canonical source membership, including source-only embeds."""
    return target[GoInfo].srcs

def go_has_source(target):
    """Returns whether the target exposes its canonical Go source view."""
    return GoInfo in target

def go_binary_nogo_dep(name, noasan = False):  # buildifier: disable=unused-variable
    """Returns the target that nogo should analyze for the given go_binary.

    Args:
        name: the go_binary rule name.
        noasan: whether the binary disables sanitizers. Unused here; the bazel
            go_binary rule is always analyzable directly.

    Returns:
        A label relative to the current package.
    """
    return ":" + name

def go_library(name, bazel_cgo = False, bazel_cdeps = [], bazel_clinkopts = [], bazel_copts = [], **kwargs):
    """Wrapper for `go_library` rule.

    Args:
        name: name of the target.
        bazel_cgo: if True, build with cgo.
        bazel_cdeps: cgo deps to pass to `go_library`.
        bazel_clinkopts: cgo linkopts to pass to `go_library`.
        bazel_copts: cgo opts to pass to `go_library`.
        **kwargs: rest of the arguments are passed to `go_library`.
    """
    importpath = kwargs.pop("importpath", "gvisor.dev/gvisor/" + native.package_name())
    _go_library(
        name = name,
        cgo = bazel_cgo,
        cdeps = bazel_cdeps,
        copts = bazel_copts,
        clinkopts = bazel_clinkopts,
        importpath = importpath,
        **kwargs
    )
    _codeql_source(name, kwargs.get("testonly"))

def go_test(name, static = False, pure = False, library = None, architectures = ["amd64", "arm64"], test_rule = None, cgroup_v2 = False, **kwargs):
    """Build a go test.

    Args:
        name: name of the output binary.
        static: build a static binary.
        pure: should it be built without cgo.
        library: the library to embed.
        architectures: Additional explicitly selected native architecture variants.
        test_rule: Optional configured raw Go test rule, including any required
            static platform transition. Used by the base and architecture variants.
        cgroup_v2: Declare an explicit cgroup-v2 environment variant for combined
            container qualification, preserving the ordinary target's environment.
        **kwargs: rest of the arguments to pass to _go_test.
    """
    if pure:
        kwargs["pure"] = "on"
    kwargs["static"] = "on" if static else _DEFAULT_STATIC
    if library:
        kwargs["embed"] = [library]
    base_gotags = kwargs.pop("gotags", [])
    kwargs["gotags"] = select({
        "//tools/bazeldefs:pagesize_64k": base_gotags + ["pagesize_64k"],
        "//tools:debug": base_gotags + ["debug"],
        "//conditions:default": base_gotags,
    }) + select({
        "//tools:lockdep": ["lockdep"],
        "//conditions:default": [],
    })
    base_rule = test_rule if test_rule != None else (_static_go_test if static else _go_test)
    kwargs["tags"] = cgroup_v1_tags(test_architecture_tags(architectures, kwargs.get("tags", [])))
    if cgroup_v2:
        kwargs["tags"] += ["rbe-has-cgroup-v2-variant"]
    base_rule(
        name = name,
        **kwargs
    )
    if cgroup_v2:
        cgroup_attributes = cgroup_v2_variant(name, base_rule, kwargs)
        if test_rule != None:
            cgroup_attributes["test_rule"] = test_rule
        test_architecture_variants(
            name + "_cgroup_v2",
            architectures,
            {"amd64": static_go_amd64_test, "arm64": static_go_arm64_test} if static else {"amd64": go_amd64_test, "arm64": go_arm64_test},
            dict(cgroup_attributes, compile_exec_compatible_with = kwargs.get("exec_compatible_with", [])),
        )
    cgroup_v1_variant(name, go_cgroup_v1_test, dict(kwargs, test_rule = base_rule))
    if test_rule != None:
        # The architecture frontend must configure the same raw rule as the
        # original declaration, without copying this macro's Go/Nogo setup.
        kwargs["test_rule"] = test_rule
    test_architecture_variants(
        name,
        architectures,
        {"amd64": static_go_amd64_test, "arm64": static_go_arm64_test} if static else {"amd64": go_amd64_test, "arm64": go_arm64_test},
        dict(kwargs, compile_exec_compatible_with = kwargs.get("exec_compatible_with", [])),
    )

def go_rule(rule, implementation, **kwargs):
    """Wraps a rule definition with Go attributes.

    Args:
      rule: rule or aspect function.
      implementation: implementation function.
      **kwargs: other arguments to pass to rule.

    Returns:
        The result of invoking the rule.
    """
    kwargs.setdefault("attrs", dict()).update({
        "_go_context_data": attr.label(default = "@io_bazel_rules_go//:go_context_data"),
        "_stdlib": attr.label(default = "@io_bazel_rules_go//:stdlib"),
    })
    if rule == aspect:
        # Nogo analyzes Go sources without invoking the C/C++ toolchain.
        kwargs.setdefault("toolchains", []).append("@io_bazel_rules_go//go:toolchain")
        return aspect(implementation, **kwargs)
    return _go_rule(implementation, **kwargs)

def go_embed_libraries(target):
    if hasattr(target.attr, "embed"):
        return target.attr.embed
    return []

def go_context(ctx, goos = None, goarch = None, attr = None):
    """Extracts a standard Go context struct.

    Args:
      ctx: the starlark context (required).
      goos: the GOOS value.
      goarch: the GOARCH value.
      attr: the analyzed rule's attributes for aspect callers.

    Returns:
      A context Go struct with pointers to Go toolchain components.
    """

    # We don't change anything for the standard library analysis. All Go files
    # are available in all instances. Note that this includes the standard
    # library sources, which are analyzed by nogo.
    go_ctx = _go_context(ctx, attr = attr, maybe_needs_cc_toolchain = False)

    nogo_args = []
    if go_ctx.mode.race:
        nogo_args = nogo_args + ["-race"]
    if go_ctx.mode.msan:
        nogo_args = nogo_args + ["-msan"]

    stdlib_mods = [f for f in go_ctx.sdk.srcs.to_list() if f.path.endswith("/src/go.mod")]
    if len(stdlib_mods) != 1:
        fail("expect exactly one stdlib src/go.mod in %s, found %s" % (go_ctx.sdk.srcs, stdlib_mods))
    stdlib_mod = stdlib_mods[0]

    return struct(
        env = dict(go_ctx.env, CGO_ENABLED = "0"),
        go = go_ctx.sdk.go,
        goarch = goarch or go_ctx.sdk.goarch,
        goos = goos or go_ctx.sdk.goos,
        gotags = go_ctx.mode.tags,
        lang_version = "go" + go_ctx.sdk.version,  # go_ctx.sdk.version excludes the go prefix.
        nogo_args = nogo_args,
        # Go builds tools such as objdump on demand, including assembly sources
        # that need the SDK's pkg/include headers.
        runfiles = depset(
            [go_ctx.sdk.go],
            transitive = [go_ctx.sdk.srcs, go_ctx.sdk.headers, go_ctx.sdk.tools, go_ctx.stdlib.libs],
        ),
        stdlib_srcs = go_ctx.sdk.srcs,
        stdlib_mod = stdlib_mod,
    )

def select_goarch():
    return select_arch(amd64 = "amd64", arm64 = "arm64")

def select_goos():
    return select_system(
        linux = "linux",
        darwin = "darwin",
    )

# Defined by rules_go.
gotsan_values = None
gotsan_flag_values = {"@io_bazel_rules_go//go/config:race": "true"}
