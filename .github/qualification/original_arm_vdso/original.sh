#!/bin/sh
set -eu
test "$(/bin/uname -m)" = aarch64
exec /usr/bin/python3 -IB "$1" "$2" "$3" /usr/bin/readelf
