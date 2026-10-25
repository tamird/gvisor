# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Build the locked source gems inside their matching, pinned official Ruby image.
require "digest"
require "fileutils"
require "json"
require "open3"
require "rubygems/package"
require "tmpdir"

architecture, output, profile_output, *inputs = ARGV
output = File.expand_path(output)
profile_output = File.expand_path(profile_output)
inputs = inputs.map { |path| File.expand_path(path) }
platform, target, machine = {
  "amd64" => ["x86_64-linux", "x86_64-linux-gnu", "Advanced Micro Devices X86-64"],
  "arm64" => ["aarch64-linux", "aarch64-linux-gnu", "AArch64"],
}.fetch(architecture)
abort "Unexpected Ruby #{RUBY_VERSION}/#{RUBY_PLATFORM}" unless RUBY_VERSION == "3.3.5" && RUBY_PLATFORM == platform
ENV["PATH"] = "/usr/local/bin:/usr/bin:/bin"
ENV.delete("BUNDLE_GEMFILE")
ENV.delete("BUNDLE_APP_CONFIG")
compiler, status = Open3.capture2e("/usr/bin/gcc", "-dumpmachine")
abort "Unexpected compiler #{compiler}" unless status.success? && compiler.strip == target

Dir.mktmpdir("ruby-http-build-") do |work|
  home = File.join(work, "opt/gems")
  ENV["HOME"] = work
  ENV["GEM_HOME"] = home
  ENV["GEM_PATH"] = home
  ENV["SOURCE_DATE_EPOCH"] = "0"
  cflags = RbConfig::CONFIG.fetch("CFLAGS") + " -ffile-prefix-map=#{work}=/usr/src/ruby-http"
  specs = inputs.map { |path| Gem::Package.new(path).spec }
  native = specs.select { |spec| !spec.extensions.empty? }.map(&:name).sort
  abort "Unexpected native extensions #{native}" unless native == ["nio4r", "puma"]
  system("/usr/local/bin/ruby", "/usr/local/bin/gem", "install", "--local",
         "--ignore-dependencies", "--no-document", "--install-dir", home,
         *inputs, "--", "--with-cflags=#{cflags}", exception: true)
  Gem.clear_paths
  specs.each do |spec|
    actual = Gem::Specification.find_by_name(spec.name, spec.version)
    abort "Unexpected installed platform" unless actual.platform == "ruby"
    actual.runtime_dependencies.each do |dependency|
      Gem::Specification.find_by_name(dependency.name, dependency.requirement)
    end
  end
  require "puma/puma_http11"
  require "nio4r"
  require "rackup"
  require "sinatra/base"
  abort "Unexpected Puma handler" unless Rackup::Handler.get(:puma).name == "Rackup::Handler::Puma"
  extensions = $LOADED_FEATURES.select { |path| path.start_with?(home + "/") && path.end_with?(".so") }
  abort "Expected two loaded native extensions: #{extensions}" unless extensions.size == 2
  files = extensions.sort.map do |path|
    header, status = Open3.capture2e("/usr/bin/readelf", "-h", "-d", path)
    abort "Unexpected ELF #{path}: #{header}" unless status.success? && header.match?(/^\s*Machine:\s+#{Regexp.escape(machine)}$/)
    abort "Extension does not link the official Ruby ABI" unless header.include?("[libruby.so.3.3]")
    {path: path.delete_prefix(work + "/"), bytes: File.size(path),
     sha256: Digest::SHA256.file(path).hexdigest, readelf: header}
  end
  logs = Dir.glob(File.join(home, "extensions/**/gem_make.out")).sort
  abort "Expected two native build logs" unless logs.length == 2
  log_records = logs.map do |path|
    record = {path: path.delete_prefix(work + "/"), bytes: File.size(path), sha256: Digest::SHA256.file(path).hexdigest}
    puts "NATIVE_GEM_BUILD_LOG #{JSON.generate(record)}"
    print File.binread(path)
    puts "NATIVE_GEM_BUILD_LOG_END"
    record
  end
  profile = {build_directory: work, compiler_flags: cflags, build_logs: log_records, ruby: RUBY_VERSION, platform: RUBY_PLATFORM, compiler_target: compiler.strip,
             sources: inputs.map { |path| {file: File.basename(path), sha256: Digest::SHA256.file(path).hexdigest} },
             gems: specs.sort_by(&:name).map { |spec| {name: spec.name, version: spec.version.to_s} },
             extensions: files,
             ruby_abi: (Dir.glob("/usr/local/include/ruby-3.3.0/**/*").select { |path| File.file?(path) } +
                        ["/usr/local/lib/ruby/3.3.0/#{platform}/rbconfig.rb", "/usr/local/lib/libruby.so.3.3.5"]).sort.map { |path|
               {path: path.delete_prefix("/"), bytes: File.size(path), sha256: Digest::SHA256.file(path).hexdigest}
             },
             tools: ["/usr/local/bin/ruby", "/usr/bin/gcc", "/usr/bin/make"].to_h { |path|
               [path, {resolved: File.realpath(path), sha256: Digest::SHA256.file(path).hexdigest}]
             }}
  File.write(profile_output, JSON.pretty_generate(profile) + "\n")
  # Keep diagnostics in the action output, not in the runtime image.
  FileUtils.rm(logs)
  FileUtils.rm_rf(File.join(home, "cache"))
  system("/usr/bin/tar", "--sort=name", "--mtime=@0", "--owner=0", "--group=0",
         "--numeric-owner", "-cf", output, "-C", work, "opt", exception: true)
end
