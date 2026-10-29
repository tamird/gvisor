#!/bin/bash
set -euo pipefail
exec sudo -n /usr/bin/unshare --mount --uts --propagation private -- /usr/bin/python3 -I "$1"
