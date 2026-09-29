"""Fork-only builds of the declared SDK's profile decoders."""

load("//tools/bazeldefs:go.bzl", "go_context", "go_rule")

def _profile_decoders_impl(ctx):
    go_ctx = go_context(ctx)
    trace = ctx.actions.declare_file(ctx.label.name + "/trace")
    pprof = ctx.actions.declare_file(ctx.label.name + "/pprof")

    # The SDK distributes these commands as source. Compile them remotely for
    # the machine holding the saved profiles; no workload rerun is needed.
    ctx.actions.run_shell(
        inputs = go_ctx.runfiles,
        outputs = [trace, pprof],
        arguments = [
            go_ctx.go.path,
            trace.path,
            pprof.path,
            go_ctx.stdlib_mod.dirname + "/..",
        ],
        env = dict(
            go_ctx.env,
            GOOS = "darwin",
            GOARCH = "arm64",
            GOMAXPROCS = "4",
            GOENV = "off",
            GOWORK = "off",
            GOTOOLCHAIN = "local",
            GOPROXY = "off",
            GOSUMDB = "off",
        ),
        command = """
set -eu
go="$PWD/$1"
trace="$PWD/$2"
pprof="$PWD/$3"
export GOROOT="$PWD/$4"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export GOCACHE="$work/cache" GOTMPDIR="$work/tmp" GOPATH="$work/gopath"
mkdir -p "$GOCACHE" "$GOTMPDIR"
cd "$GOROOT/src"
"$go" build -p=4 -trimpath -buildvcs=false -o "$trace" cmd/trace
"$go" build -p=4 -trimpath -buildvcs=false -o "$pprof" cmd/pprof
""",
        mnemonic = "ProfileDecoders",
        progress_message = "Building declared Go profile decoders for Darwin ARM64",
    )
    return [DefaultInfo(files = depset([trace, pprof]))]

profile_decoders = go_rule(
    rule,
    implementation = _profile_decoders_impl,
)
