"""List the worker root and actually execute the conventional host tool paths."""

def _observed_root_impl(ctx):
    output = ctx.actions.declare_file(ctx.label.name + ".txt")
    script = """
set -u
out=$1
{
    echo "kernel_root"
    printf 'machine %s\\n' "$("$2" uname -m)"
    if [ -f /hello ]; then
        value=$("$2" sha256sum /hello) || exit 1
        printf 'hello-sha %s\\n' "${value%% *}"
    fi
    for p in /* /.[!.]*; do [ ! -e "$p" ] || printf 'root %s\\n' "$p"; done
    printf 'env PATH=%s\\n' "${PATH-<unset>}"
    printf 'env TMPDIR=%s\\n' "${TMPDIR-<unset>}"
    for p in /etc/* /etc/.[!.]*; do
        [ -e "$p" ] || continue
        case "$p" in
            /etc/hosts|/etc/hostname|/etc/resolv.conf|/etc/mtab) printf 'etc %s\\n' "${p##*/}" ;;
            *) echo 'etc <other>' ;;
        esac
    done
    while IFS=' ' read -r id parent dev root mount rest; do
        case "$mount" in
            /|/proc|/sys|/dev|/dev/pts|/dev/mqueue|/dev/shm|/sys/fs/cgroup|/etc/hosts|/etc/hostname|/etc/resolv.conf) ;;
            "$PWD") mount="<execroot>" ;;
            /sys/*|/proc/*|/dev/*) continue ;;
            *) mount="<other>" ;;
        esac
        fstype=${rest#* - }; fstype=${fstype%% *}
        printf 'mount %s %s\\n' "$mount" "$fstype"
    done < /proc/self/mountinfo
    if [ -d /tmp ]; then echo "tmp present"; else echo "tmp absent"; fi
    for p in /bin/sh /bin/bash /usr/bin/cc /usr/bin/gcc /usr/bin/clang; do
        x=no
        if [ -x "$p" ]; then x=yes; fi
        case "$p" in
            */sh|*/bash) "$p" -c 'exit 0' > "$out.tmp" 2>&1; rc=$? ;;
            *) "$p" --version > "$out.tmp" 2>&1; rc=$? ;;
        esac
        printf 'exec %s %s %s\\n' "$p" "$x" "$rc"
    done
} > "$out"
"""
    ctx.actions.run(
        executable = ctx.file.tool,
        arguments = ["ash", "-c", script, "root-probe", output.path, ctx.file.tool.path],
        outputs = [output],
        mnemonic = "ObserveWorkerRoot",
        toolchain = None,
    )
    return [DefaultInfo(files = depset([output]))]

observed_root = rule(
    implementation = _observed_root_impl,
    attrs = {"tool": attr.label(default = "@bootstrap_busybox_arm64//:payload/usr/bin/busybox", allow_single_file = True, cfg = "exec")},
)
