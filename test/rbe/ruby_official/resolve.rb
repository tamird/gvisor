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

require "bundler"
require "digest"
require "fileutils"
require "json"
require "open3"
require "tmpdir"

abort "Expected official Ruby 3.3.5" unless RUBY_VERSION == "3.3.5"
abort "Expected native AMD64 Ruby" unless RUBY_PLATFORM == "x86_64-linux"
abort "Expected bundled Bundler 2.5.16" unless Bundler::VERSION == "2.5.16"
gemfile, output = ARGV.map { |path| File.expand_path(path) }
# These absolute tools and their dependencies belong to the pinned execution image.
ENV["PATH"] = "/usr/local/bin:/usr/bin:/bin"
ENV.delete("BUNDLE_GEMFILE")
ENV.delete("BUNDLE_APP_CONFIG")

Dir.mktmpdir("ruby-http-resolve-") do |work|
  Dir.chdir(work) do
    ENV["HOME"] = work
    FileUtils.cp(gemfile, "Gemfile")
    system("/usr/local/bin/ruby", "/usr/local/bin/bundle", "lock",
           "--add-platform", "x86_64-linux", "aarch64-linux", exception: true)
    lock = Bundler::LockfileParser.new(File.read("Gemfile.lock"))
    specs = lock.specs.sort_by(&:name)
    abort "Expected source gems only" unless specs.all? { |spec| spec.platform == "ruby" }
    rows = specs.map do |spec|
      system("/usr/local/bin/ruby", "/usr/local/bin/gem", "fetch", spec.name,
             "--version", spec.version.to_s, "--platform", "ruby",
             "--clear-sources", "--source", "https://rubygems.org", exception: true)
      name = "#{spec.name}-#{spec.version}.gem"
      {name: spec.name, version: spec.version.to_s, file: name,
       bytes: File.size(name), sha256: Digest::SHA256.file(name).hexdigest,
       url: "https://rubygems.org/downloads/#{name}"}
    end
    compiler, status = Open3.capture2e("/usr/bin/gcc", "-dumpmachine")
    abort "Expected native AMD64 compiler: #{compiler}" unless status.success? && compiler.strip == "x86_64-linux-gnu"
    profile = {ruby: RUBY_VERSION, platform: RUBY_PLATFORM, bundler: Bundler::VERSION,
               compiler_target: compiler.strip, gems: rows,
               tools: ["/usr/local/bin/ruby", "/usr/bin/gcc", "/usr/bin/make"].to_h { |path|
                 [path, {resolved: File.realpath(path), sha256: Digest::SHA256.file(path).hexdigest}]
               }}
    File.write("resolved.json", JSON.pretty_generate(profile) + "\n")
    system("/usr/bin/tar", "--sort=name", "--mtime=@0", "--owner=0", "--group=0",
           "--numeric-owner", "-cf", output, "Gemfile", "Gemfile.lock", "resolved.json",
           *rows.map { |row| row[:file] }, exception: true)
  end
end
