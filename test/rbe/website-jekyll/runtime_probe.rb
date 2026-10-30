# Fork-only proof against the actual Jekyll and proofer using only Bazel inputs.
require "fileutils"
require "json"
require "tmpdir"
require "rubygems"
require "rubygems/dependency_installer"

$LOAD_PATH.map! { |path| File.expand_path(path) }
out, gems, loader, lib, node, checks = ARGV
out, gems, loader, node, checks = [out, gems, loader, node, checks].map { |path| File.expand_path(path) }
libraries = lib.split(":").map { |path| File.expand_path(path) }.join(":")
base_gems = ENV.fetch("GEM_PATH").split(":").map { |path| File.expand_path(path) }
raise "worker has host tools" if File.exist?("/bin/sh") || File.exist?("/usr/bin/env") || File.exist?("/usr/bin/node")
Dir.mktmpdir("site-", File.expand_path(".")) do |work|
  ENV["TMPDIR"] = work
  ENV["HOME"] = work
  new_gems = File.join(work, "gems")
  FileUtils.mkdir_p(new_gems)
  Gem.use_paths(new_gems, [new_gems] + base_gems)
  Gem.sources.clear
  installer = Gem::DependencyInstaller.new(domain: :local, document: [], wrappers: false)
  Dir.chdir(gems) do
    Dir["*.gem"].each do |path|
      spec = Gem::Package.new(path).spec
      next unless Gem::Specification.find_all_by_name(spec.name, spec.version.to_s).empty?
      installer.install(spec.name, spec.version)
    end
  end
  require "execjs"
  ExecJS.runtime = ExecJS::ExternalRuntime.new(
    name: "Declared Node",
    command: "#{loader} --library-path #{libraries} #{node}",
    runner_path: ExecJS.root + "/support/node_runner.js",
    encoding: "UTF-8",
  )
  raise "V8 did not execute" unless ExecJS.eval("6 * 7") == 42
  require "jekyll"
  plugin_names = %w[jekyll-paginate jekyll-autoprefixer jekyll-inline-svg jekyll-relative-links jekyll-feed jekyll-sitemap]
  plugin_names.each { |name| require name }
  require "webrick"
  source = File.join(work, "source")
  FileUtils.mkdir_p(source)
  File.write(File.join(source, "_config.yml"), "url: https://gvisor.dev\nplugins: #{plugin_names}\n")
  File.write(File.join(source, "index.md"), "---\n---\n# gVisor website\n")
  File.write(File.join(source, "main.scss"), "---\n---\na { display: flex; }\n")
  dest = File.join(work,"site")
  Jekyll::Site.new(Jekyll.configuration("source" => source, "destination" => dest, "disable_disk_cache" => true)).process
  raise "missing page" unless File.read(File.join(dest,"index.html")).include?("gVisor website")
  raise "missing stylesheet" unless File.read(File.join(dest,"main.css")).include?("display:")
  require checks
  valid = File.join(work, "proof")
  FileUtils.mkdir_p(valid)
  File.write(File.join(valid,"index.html"), '<!doctype html><html><head><title>test</title></head><body><a href="#ok" target="_blank" rel="noopener" id="ok">ok</a></body></html>')
  HTMLProofer.check_directories([valid], check_html: true, check_favicon: false, disable_external: true).run
  File.write(out,JSON.pretty_generate({
    ruby:RUBY_DESCRIPTION, jekyll:Jekyll::VERSION, node:ExecJS.runtime.name,
    no_worker_shell:!File.exist?("/bin/sh"), plugin_names:plugin_names,
    gems:Gem.loaded_specs.transform_values { |spec| spec.version.to_s },
  }))
end
