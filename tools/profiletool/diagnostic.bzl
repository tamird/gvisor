"""Fork-only builds of the declared SDK's CPU profile decoder."""

load("//tools/bazeldefs:go.bzl", "go_context", "go_rule")

def _profile_decoder_impl(ctx):
    go_ctx = go_context(ctx)
    pprof = ctx.actions.declare_file(ctx.label.name + "/pprof")

    # The SDK distributes pprof as source. Compile it remotely for
    # the remote analysis parent; no workload rerun is needed.
    ctx.actions.run_shell(
        inputs = go_ctx.runfiles,
        outputs = [pprof],
        arguments = [
            go_ctx.go.path,
            pprof.path,
            go_ctx.stdlib_mod.dirname + "/..",
        ],
        env = dict(
            go_ctx.env,
            GOOS = "linux",
            GOARCH = "amd64",
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
pprof="$PWD/$2"
export GOROOT="$PWD/$3"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export GOCACHE="$work/cache" GOTMPDIR="$work/tmp" GOPATH="$work/gopath"
mkdir -p "$GOCACHE" "$GOTMPDIR"
cd "$GOROOT/src"
"$go" build -p=4 -trimpath -buildvcs=false -o "$pprof" cmd/pprof
""",
        mnemonic = "ProfileDecoder",
        progress_message = "Building declared Go CPU profile decoder for Linux AMD64",
    )
    return [DefaultInfo(files = depset([pprof]))]

profile_decoder = go_rule(
    rule,
    implementation = _profile_decoder_impl,
)
