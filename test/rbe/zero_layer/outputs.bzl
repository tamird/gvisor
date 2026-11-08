"""Expose both existing stdlib outputs and observe the native worker's filesystem."""

load("@io_bazel_rules_go//go/private:providers.bzl", "GoStdLib")

def _stdlib_outputs_impl(ctx):
    stdlib = ctx.attr.stdlib[GoStdLib]
    return [DefaultInfo(files = depset([stdlib._list_json], transitive = [stdlib.libs]))]

stdlib_outputs = rule(
    implementation = _stdlib_outputs_impl,
    attrs = {"stdlib": attr.label(default = "@io_bazel_rules_go//:stdlib", providers = [GoStdLib])},
)

def _root_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".txt")
    tool = ctx.file.tool
    # Use the declared BusyBox applet even when the root has no /bin/sh.
    script = """
set -eu
machine=$("$1" uname -m)
case "$machine" in
    x86_64) field=vendor_id ;;
    aarch64) field='CPU implementer' ;;
    *) exit 1 ;;
esac
"$1" grep -Eq "^$field[[:space:]]*:" /proc/cpuinfo
{
    echo "$machine"
    for path in /bin/sh /usr/bin/cc /hello /tmp; do
        if [ -e "$path" ]; then echo "$path present"; else echo "$path absent"; fi
    done
} > "$2"
"""
    ctx.actions.run(
        executable = tool,
        arguments = ["ash", "-c", script, "observe-root", tool.path, out.path],
        outputs = [out],
        mnemonic = "ObserveWorkerRoot",
        toolchain = None,
    )
    return [DefaultInfo(files = depset([out]))]

root_probe = rule(
    implementation = _root_impl,
    attrs = {"tool": attr.label(mandatory = True, allow_single_file = True, cfg = "exec")},
)
