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

require "fileutils"
require "jekyll"
require "rouge"

source, destination = ARGV.map { |path| File.expand_path(path) }
File.open(File.join(source, "_sass", "syntax.css"), "w") do |css|
  Rouge::Theme.find("github").new.render { |line| css.puts(line) }
end

# Use the site's publication timezone and relative Sass source paths.
ENV["TZ"] = "America/Chicago"
work = File.join(File.dirname(source), "srv", "jekyll")
FileUtils.mkdir_p(work)
config = Jekyll.configuration("source" => source, "destination" => destination, "future" => true)
Dir.chdir(work) { Jekyll::Site.new(config).process }
