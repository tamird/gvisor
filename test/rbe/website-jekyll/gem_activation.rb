# Qualification only. Write to stderr because rougify writes CSS to stdout.
at_exit do
  versions = Gem.loaded_specs.to_h { |name, spec| [name, spec.version.to_s] }
  require "json"
  warn "GVISOR_GEMS " + JSON.generate({program: $PROGRAM_NAME, gems: versions})
end
