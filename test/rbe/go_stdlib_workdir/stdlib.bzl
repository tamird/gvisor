"""Expose the existing go-list output beside a normal product package build."""

load("@io_bazel_rules_go//go/private:providers.bzl", "GoStdLib")

def _metadata_impl(ctx):
    return [DefaultInfo(files = depset([ctx.attr.stdlib[GoStdLib]._list_json]))]

stdlib_metadata = rule(
    implementation = _metadata_impl,
    attrs = {"stdlib": attr.label(default = "@io_bazel_rules_go//:stdlib", providers = [GoStdLib])},
)

def _tmp_action_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".txt")
    shell = ctx.file._tool
    # Leave a marker on purpose. The dependent action verifies that it starts
    # with a fresh /tmp even after its predecessor has completed successfully.
    script = """
set -eu
bb=$1
out=$2
role=$3
previous=$4
marker=/tmp/worker-tmp-isolation-sentinel
test -z "$("$bb" ls -A /tmp)"
test ! -e /fixture-missing-tmpdir
test "$("$bb" stat -c '%a' /tmp)" = 1777
test ! -e "$marker"
for path in /bin/sh /bin/bash /usr/bin/env /usr/bin/cc /usr/bin/gcc /usr/bin/clang; do
    if "$path" --version >/dev/null 2>&1; then exit 1; else test "$?" -eq 127; fi
done
case "$role" in
    leave) printf '%s' sentinel > "$marker"; test -s "$marker" ;;
    inspect) test -s "$previous"; dir=$("$bb" mktemp -d /tmp/go-XXXXXX); "$bb" rmdir "$dir" ;;
    *) exit 1 ;;
esac
{
    echo "$role"
    "$bb" uname -m
    "$bb" stat -c 'mode=%a' /tmp
    echo "initially_empty=true"
    echo "ambient_tools=absent"
    echo "marker_absent_at_start=true"
    for path in /*; do echo "root=$path"; done
} > "$out"
"""
    previous = ctx.file.previous
    ctx.actions.run(
        executable = shell,
        inputs = [previous] if previous else [],
        arguments = ["ash", "-c", script, "probe", shell.path, out.path, ctx.label.name, previous.path if previous else ""],
        outputs = [out],
        mnemonic = "ProbeWorkerTmp",
        toolchain = None,
    )
    return [DefaultInfo(files = depset([out]))]

tmp_action = rule(
    implementation = _tmp_action_impl,
    attrs = {
        "previous": attr.label(allow_single_file = True),
        "_tool": attr.label(default = "@bootstrap_busybox_arm64//:payload/usr/bin/busybox", allow_single_file = True, cfg = "exec"),
    },
)
