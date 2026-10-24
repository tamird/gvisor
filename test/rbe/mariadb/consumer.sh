#!/bin/bash
set -euo pipefail
image=gvisor.dev/images/compatibility/mariadb/mariadb:latest
expected=sha256:57c69e064d6c79e7cbe8c77a3342327d13455947616e7e7908bda2fbad348e34
actual=$(docker image inspect --format '{{.Id}}' "$image")
[[ "$actual" == "$expected" ]]
printf 'MARIADB_LOADED_CONFIG_ID %s\n' "$actual"
docker image inspect "$image"
exec "$@"
