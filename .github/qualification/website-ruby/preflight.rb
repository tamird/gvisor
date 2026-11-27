require "json"
require "fileutils"
require "tmpdir"
require "jekyll"
require "rouge"
require "jekyll-autoprefixer"
require_relative "html_checks"
workspace = ENV.fetch("BUILD_WORKSPACE_DIRECTORY")
result = ENV.fetch("WEBSITE_RUBY_RESULTS")
raise "Ruby version changed" unless RUBY_VERSION == "3.4.11"
raise "Node version changed" unless `node --version`.strip == "v12.15.0"
report = {ruby: RUBY_VERSION, execjs: ExecJS.runtime.name, gems: Gem.loaded_specs.transform_values { |s| s.version.to_s }}
Dir.mktmpdir("website-style-") do |work|
  input, output = File.join(work, "input"), File.join(work, "output")
  FileUtils.mkdir_p(File.join(input, "css"))
  FileUtils.cp_r(File.join(workspace, "website/_sass"), input)
  File.write(File.join(input, "css/main.scss"), "---\n---\n" + File.read(File.join(workspace, "website/css/main.scss")))
  File.open(File.join(input, "_sass/syntax.css"), "w") { |f| Rouge::Theme.find("github").new.render { |line| f.puts(line) } }
  calls = 0
  original = AutoprefixerRails.method(:process)
  AutoprefixerRails.define_singleton_method(:process) { |*args| calls += 1; original.call(*args) }
  config = Jekyll.configuration("config" => File.join(workspace, "website/_config.yml"), "source" => input, "destination" => output, "plugins" => ["jekyll-autoprefixer"], "future" => true)
  Jekyll::Site.new(config).process
  css = File.read(File.join(output, "css/main.css"))
  raise "Autoprefixer was not called" unless calls > 0
  raise "Current stylesheet missing" unless css.include?("display") && css.include?("transform")
  report[:stylesheet_bytes], report[:autoprefixer_calls] = css.bytesize, calls
  File.write(File.join(result, "main.css"), css)
  File.write(File.join(output, "favicon.ico"), "")
  html = '<!doctype html><html><head><title>Preflight</title><link rel="icon" href="/favicon.ico"></head><body><a href="#target" target="_blank" rel="noopener">local</a><p id="target">target</p></body></html>'
  File.write(File.join(output, "index.html"), html)
  options = {check_html: true, check_favicon: true, disable_external: true}
  positive = HTMLProofer.check_directories([output], options)
  positive.run
  raise "Valid HTML rejected" unless positive.failures.empty?
  File.write(File.join(output, "index.html"), html.sub(' rel="noopener"', ''))
  negative = HTMLProofer.check_directories([output], options)
  negative.check_files
  raise "Missing noopener accepted" unless negative.failures.any? { |f| f.to_s.include?("noopener") }
  report[:html_positive], report[:html_negative] = "PASS", "PASS"
end
File.write(File.join(result, "result.json"), JSON.pretty_generate(report) + "\n")
puts JSON.generate(report)
