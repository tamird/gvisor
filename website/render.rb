# musl searches these directories again when a plugin opens a native extension.
# Resolve them before Jekyll changes directory to generate relative Sass URLs.
unless ARGV.fetch(2).start_with?("/")
  inputs, dest, loader, libraries, node, checks = ARGV
  sdk = File.dirname(File.dirname(loader))
  # A local Bazel sandbox symlinks the declared SDK files into its execroot.
  # Ruby resolves relative requires through the underlying files too. Use one
  # spelling throughout so RubyGems and Jekyll load each feature exactly once.
  ruby = File.realpath(File.join(sdk, "usr/local/bin/ruby"))
  runtime = File.expand_path("../../..", File.dirname(ruby))
  sdk_path = ->(path) { File.join(runtime, path.delete_prefix(sdk + "/")) }
  loader = sdk_path.call(loader)
  libraries = libraries.split(":").map(&sdk_path).join(":")
  %w[GEM_PATH RUBYLIB].each { |key| ENV[key] = ENV.fetch(key).split(":").map(&sdk_path).join(":") }
  exec(loader, "--library-path", libraries, ruby, File.expand_path(__FILE__),
       inputs, dest, loader, libraries, node, checks)
end

require "fileutils"
require "json"
require "tmpdir"
require "rubygems/dependency_installer"
require "time"

$LOAD_PATH.map! { |path| File.expand_path(path) }
inputs, dest, loader, libraries, node, checks = ARGV
inputs = JSON.parse(File.read(inputs))
dest, loader, node, checks = [dest, loader, node, checks].map { |f| File.expand_path(f) }
libraries = libraries.split(":").map { |path| File.expand_path(path) }.join(":")
base_gems = Gem.path.map { |path| File.expand_path(path) }

def stage(source, relative, path)
  target = File.join(source, relative)
  FileUtils.mkdir_p(File.dirname(target))
  FileUtils.cp(path, target)
  target
end

def stage_document(source, doc)
  path = doc.fetch("file")
  unless path.end_with?(".md")
    stage(source, File.join(doc.fetch("permalink").delete_prefix("/"), File.basename(path)), path)
    return
  end
  dest = doc.fetch("layout") == "post" ? File.join("_posts", File.basename(path)) : doc.fetch("short")
  target = File.join(source, dest)
  FileUtils.mkdir_p(File.dirname(target))
  lines = File.readlines(path)
  title_index = lines.index { |line| line.start_with?("# ") }
  title = title_index ? lines.delete_at(title_index).delete_prefix("#").chomp : ""
  File.open(target, "w") do |f|
    f.puts("---", "title: #{title.to_json}", "excerpt_separator: '<!--/excerpt-->'")
    %w[description permalink category subcategory weight editpath authors layout include_in_menu body_classes].each do |k|
      v = doc.fetch(k)
      f.puts("#{k}: #{v.is_a?(Array) ? v.to_json : v}")
    end
    f.puts("---")
    lines.each { |line| f.write(line.chomp == "[TOC]" ? "- TOC\n{:toc}\n" : line) }
  end
end

Dir.mktmpdir("website-", File.expand_path(".")) do |work|
  ENV["TMPDIR"] = work
  ENV["HOME"] = work
  new_gems = File.join(work, "gems")
  gem_cache = File.join(work, "downloads")
  FileUtils.mkdir_p([new_gems, gem_cache])
  Gem.use_paths(new_gems, [new_gems] + base_gems)
  Gem.sources.clear
  inputs.fetch("gems").each { |f| FileUtils.cp(f, gem_cache) }
  # Native extensions are prebuilt in the pinned runtime. Install the remaining
  # pure-Ruby gems from the declared archives, with network sources disabled.
  installer = Gem::DependencyInstaller.new(domain: :local, document: [], wrappers: false)
  Dir.chdir(gem_cache) do
    Dir["*.gem"].each do |file|
      spec = Gem::Package.new(file).spec
      installer.install(spec.name, spec.version) if Gem::Specification.find_all_by_name(spec.name, spec.version.to_s).empty?
    end
  end
  ENV["PATH"] = File.dirname(node)
  require "execjs"
  ExecJS.runtime = ExecJS::ExternalRuntime.new(name: "Node", command: "#{loader} --library-path #{libraries} #{node}",
                                               runner_path: ExecJS.root + "/support/node_runner.js", encoding: "UTF-8")
  ENV["PATH"] = ""
  require "jekyll"
  require "rouge"
  source = File.join(work, "input")
  inputs.fetch("site").each do |file, relative|
    stage(source, relative, file)
  end
  inputs.fetch("docs").each { |doc| stage_document(source, doc) }
  stage(source, "css.tar", inputs.fetch("css"))
  FileUtils.cp_r(File.join(inputs.fetch("syscalls"), "."), source)
  File.open(File.join(source, "_sass", "syntax.css"), "w") do |css|
    Rouge::Theme.find("github").new.render { |line| css.puts(line) }
  end
  # Match the official image's TZ and working-directory-relative Sass sources.
  ENV["TZ"] = File.join(File.dirname(File.dirname(loader)), "usr/share/zoneinfo/America/Chicago")
  jekyll_workdir = File.join(work, "srv", "jekyll")
  FileUtils.mkdir_p(jekyll_workdir)
  # Without an explicit publication time, Bazel's volatile status preserves
  # the preview date until the action would otherwise run again.
  time = inputs.fetch("as_of")
  if time.empty?
    stamp = File.readlines(inputs.fetch("workspace_status")).find { |line| line.start_with?("BUILD_TIMESTAMP ") }
    time = Time.at(Integer(stamp.split.fetch(1)))
  end
  config = Jekyll.configuration("source" => source, "destination" => dest, "future" => true, "time" => time)
  # Jekyll sorts pages only by basename. Give equal names a stable order so
  # the sitemap does not depend on how the execution filesystem lists them.
  Jekyll::Hooks.register(:site, :post_read) { |site| site.pages.sort_by! { |page| [page.name, page.path] } }

  Dir.chdir(jekyll_workdir) { Jekyll::Site.new(config).process }
  inputs.fetch("static").each { |file, relative| stage(dest, relative, file) }
  require checks
  check_site(dest)
end
