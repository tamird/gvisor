"""Present the production SDK as a relative go symlink, retaining its real inputs."""

load("@io_bazel_rules_go//go/private:providers.bzl", "GoSDK")

def _relative_sdk_impl(ctx):
    sdk = ctx.attr.sdk[GoSDK]
    link = ctx.actions.declare_symlink(ctx.label.name + "/bin/go")
    target = "../" * len(link.dirname.split("/")) + sdk.go.path
    ctx.actions.symlink(output = link, target_path = target)

    utilities = []
    for name in ctx.attr.helpers:
        tool = ctx.actions.declare_file(ctx.label.name + "/bin/helpers/" + name)
        ctx.actions.symlink(output = tool, target_file = ctx.file._busybox, is_executable = True)
        utilities.append(tool)

    witness = ctx.actions.declare_file(ctx.label.name + "/link.txt")
    ctx.actions.run(
        executable = ctx.file._busybox,
        arguments = [
            "ash",
            "-ec",
            """test -L "$1"
test "$1" -ef "$2"
actual=$("$0" readlink "$1")
test "$actual" = "$3"
case "$actual" in /*) exit 1;; esac
{
    printf 'relative-target=%s\\n' "$actual"
    for util in readlink dirname; do
        if command -v "$util" >/dev/null 2>&1; then
            printf 'default-%s=found\\n' "$util"
        else
            printf 'default-%s=missing\\n' "$util"
        fi
    done
} > "$4"
""",
            ctx.file._busybox.path,
            link.path,
            sdk.go.path,
            target,
            witness.path,
        ],
        inputs = [link, sdk.go],
        outputs = [witness],
        mnemonic = "WitnessGoSDKLink",
        toolchain = None,
    )
    return [
        DefaultInfo(files = depset([link])),
        OutputGroupInfo(witness = depset([witness])),
        GoSDK(
            experiments = sdk.experiments,
            go = link,
            goarch = sdk.goarch,
            goos = sdk.goos,
            headers = sdk.headers,
            libs = sdk.libs,
            package_list = sdk.package_list,
            root_file = sdk.root_file,
            srcs = sdk.srcs,
            tools = depset([sdk.go, witness] + utilities, transitive = [sdk.tools]),
            version = sdk.version,
        ),
    ]

relative_sdk = rule(
    implementation = _relative_sdk_impl,
    attrs = {
        "helpers": attr.string_list(),
        "sdk": attr.label(mandatory = True, providers = [GoSDK]),
        "_busybox": attr.label(
            default = "@bootstrap_busybox//:payload/usr/bin/busybox",
            allow_single_file = True,
        ),
    },
)
