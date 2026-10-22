#!/bin/sh
# Upstream Make assigns these tools unconditionally; environment values alone
# would fall back to gcc/ar/nm. Command-line assignments also reach sub-makes.
set -eu
exec "$@" "CC=$CC" "AR=$AR" "HOSTCC=$BUSYBOX_HOSTCC" "NM=$BUSYBOX_NM"
