#!/bin/sh
# Upstream Make assigns these tools unconditionally; environment values alone
# would fall back to gcc/ar/nm. Command-line assignments also reach sub-makes.
set -eu
# foreign_cc's make rule symlinks source directories. BusyBox's generator
# discovers real directories and writes Config.in/Kbuild files inside them.
# Materialize those directories in the private build tree before either target.
for directory in */; do
    directory=${directory%/}
    if [ -L "$directory" ]; then
        test ! -e "$directory.busybox-copy"
        cp -L -R -- "$directory" "$directory.busybox-copy"
        rm -- "$directory"
        mv -- "$directory.busybox-copy" "$directory"
    fi
done
exec "$@" "CC=$CC" "AR=$AR" "HOSTCC=$BUSYBOX_HOSTCC" "NM=$BUSYBOX_NM"
