#!/bin/bash
set -euo pipefail
archive=$1
shift
printf 'RUBY_IMAGE_ARCHIVE '
sha256sum "$archive"
printf 'RUBY_LOADED_IMAGE '
docker image inspect --format '{{json .}}' gvisor.dev/images/image-test/ruby:latest
exec "$@"
