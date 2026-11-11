#!/bin/bash
# Fork qualification for the pinned Fastlane benchmark image.
set -euo pipefail
image=gvisor.dev/images/benchmarks/rubydev:latest
cat > "$TEST_TMPDIR/clock.rb" <<'RUBY'
module AcrossSecond
  def decode(...)
    payload, header = super
    entered = Time.now
    sleep(0.01) while Time.now.to_i == entered.to_i
    puts "DECODE_BOUNDARY issued=#{payload.fetch('iat')} decoded=#{entered.to_i} checked=#{Time.now.to_i}"
    [payload, header]
  end
end

RSpec.configure do |config|
  config.before(:example) do
    require "jwt"
    JWT.singleton_class.prepend(AcrossSecond)
  end
end
RUBY
chmod 644 "$TEST_TMPDIR/clock.rb"
docker run --rm --runtime=runc -i -w /fastlane "$image" sh -es <<'INSIDE'
export SPEC_OPTS='--dry-run --format json --out /tmp/examples.json'
bash /files/run_fastlane_tests.sh >/dev/null
ruby -rjson -rzlib -rbase64 -e 'e=JSON.parse(File.read("/tmp/examples.json")).fetch("examples").map {|x| [x.fetch("file_path"),x.fetch("full_description")]}.sort
puts "RUBY_EXAMPLES "+Base64.strict_encode64(Zlib.deflate(JSON.generate(e)))
puts "RUBY_VERSION #{RUBY_VERSION} EXAMPLES #{e.length}"'
INSIDE
docker run --rm --runtime=runc -i -w /fastlane \
  --mount "type=bind,src=$TEST_TMPDIR/clock.rb,dst=/clock.rb,readonly" \
  "$image" bundle exec rspec spaceship/spec/connect_api/token_spec.rb \
    --require /clock.rb --example 'generates proper token'
