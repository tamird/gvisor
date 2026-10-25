#!/bin/bash
set -euo pipefail
archive=$1
shift
runtime=${2#--runtime=}
case "$runtime" in runc|runsc) ;; *) echo 'Expected an explicit Docker runtime' >&2; exit 1 ;; esac
printf 'RUBY_IMAGE_ARCHIVE '
sha256sum "$archive"
printf 'RUBY_LOADED_IMAGE '
docker image inspect --format '{{json .}}' gvisor.dev/images/image-test/ruby:latest
docker run --rm --runtime="$runtime" --entrypoint=/usr/local/bin/ruby gvisor.dev/images/image-test/ruby:latest -rjson -e '
require "puma"
require "nio"
require "rackup"
require "sinatra/base"
expected = {"puma" => "6.6.0", "nio4r" => "2.7.3", "rackup" => "2.1.0", "sinatra" => "4.1.1"}
versions = expected.keys.to_h { |name| [name, Gem.loaded_specs.fetch(name).version.to_s] }
abort "Unexpected Ruby/gem versions: #{RUBY_VERSION} #{versions}" unless RUBY_VERSION == "3.3.5" && versions == expected
abort "Puma handler unavailable" unless Rackup::Handler.get(:puma) == Rackup::Handler::Puma
extensions = $LOADED_FEATURES.grep(/puma_http11|nio4r_ext/)
abort "Missing native extensions: #{extensions}" unless extensions.length == 2 && extensions.all? { |path| path.end_with?(".so") }
puts "RUBY_RUNTIME_PROFILE " + JSON.generate({ruby: RUBY_VERSION, platform: RUBY_PLATFORM, architecture: RbConfig::CONFIG.fetch("arch"), gems: versions, native_extensions: extensions})
'
exec "$@"
