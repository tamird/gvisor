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

raise "undeclared worker tool" if File.exist?("/bin/sh") || File.exist?("/usr/bin/node")
Dir.mktmpdir("website-", File.expand_path(".")) do |work|
  ENV["TMPDIR"] = work
  ENV["HOME"] = work
  new_gems = File.join(work, "gems")
  gem_cache = File.join(work, "downloads")
  FileUtils.mkdir_p([new_gems, gem_cache])
  Gem.use_paths(new_gems, [new_gems] + base_gems)
  Gem.sources.clear
  inputs.fetch("gems").each { |f| FileUtils.cp(f, gem_cache) }
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
  source = File.join(work, "source")
  inputs.fetch("site").each do |file, relative|
    target = stage(source, relative, file)
    # The Jekyll stylesheet must have front matter to be processed.
    File.write(target, "---\n---\n" + File.read(file)) if relative == "css/main.scss"
  end
  inputs.fetch("docs").each { |doc| stage_document(source, doc) }
  stage(source, "css.tar", inputs.fetch("legacy_css_tar"))
  FileUtils.cp_r(File.join(inputs.fetch("syscalls"), "."), source)
  File.open(File.join(source, "_sass", "syntax.css"), "w") do |css|
    Rouge::Theme.find("github").new.render { |line| css.puts(line) }
  end
  config = Jekyll.configuration("source" => source, "destination" => dest, "future" => true, "time" => inputs.fetch("as_of"))
  Jekyll::Site.new(config).process
  inputs.fetch("static").each { |file, relative| stage(dest, relative, file) }
  require checks
  HTMLProofer.check_directories([dest], check_html: true, check_favicon: true, disable_external: true).run
end
