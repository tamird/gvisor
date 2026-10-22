#!/bin/sh
# Upstream Make assigns these tools unconditionally; environment values alone
# would fall back to gcc/ar/nm. Command-line assignments also reach sub-makes.
set -eu
# foreign_cc's make rule symlinks source directories. BusyBox's generator
# discovers real directories and writes Config.in/Kbuild files inside them.
# Materialize those directories in the private build tree before configuration.
for directory in */; do
    directory=${directory%/}
    if [ -L "$directory" ]; then
        test ! -e "$directory.busybox-copy"
        cp -L -R -- "$directory" "$directory.busybox-copy"
        rm -- "$directory"
        mv -- "$directory.busybox-copy" "$directory"
    fi
done
# mkconfigs and usage_compressed call bzip2 by name.
PATH="${BUSYBOX_BZIP2%/*}:$PATH"
export PATH
# BusyBox allnoconfig resets even KCONFIG_ALLCONFIG's explicit booleans. Apply
# the small fragment afterward, then let upstream defconfig resolve selections.
target=$2
set -- "$@" "KBUILD_DEFCONFIG=.config" "CC=$CC" "AR=$AR" "HOSTCC=$BUSYBOX_HOSTCC" "NM=$BUSYBOX_NM"
if [ "$target" = defconfig ]; then
    while IFS= read -r setting; do
        case "$setting" in
            CONFIG_*=*)
                name=${setting%%=*}
                sed -i "/^$name=/d; /^# $name is not set$/d" .config
                printf '%s\n' "$setting" >> .config
                ;;
        esac
    done < "$BUSYBOX_CONFIG"
    "$@"
    # Reject unsatisfied dependencies before compiling the runtime payload.
    while IFS= read -r setting; do
        case "$setting" in
            CONFIG_*=*)
                if ! grep -Fqx -- "$setting" .config; then
                    printf 'BusyBox did not retain required setting: %s\n' "$setting" >&2
                    exit 1
                fi
                ;;
        esac
    done < "$BUSYBOX_CONFIG"
    exit 0
fi
exec "$@"
