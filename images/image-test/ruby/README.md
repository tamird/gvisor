# Ruby HTTP test image

This image runs the existing Sinatra server on the pinned official Ruby
3.3.5 image. The twelve source gems are pinned by SHA-256 in MODULE.bazel;
Gemfile.lock records their Bundler resolution. The build installs only
those local gems and compiles Puma and nio4r inside each architecture's
pinned Ruby image, using its matching headers, library, and compiler.
Remote actions require native AMD64 or ARM64 workers and disable network
access. The application does not install gems at startup.

The native build maps temporary paths in debug information to
/usr/src/ruby-http, retains the source files and notices, and puts full
build logs in action output. The generated tar and JSON profile normalize timestamps and temporary
paths. The profile records the
actual Ruby ABI, compiler, gem hashes, and native ELF identities.

licenses.json binds the selected terms to notice bytes inside each
pinned gem and their matching upstream source. The image retains those
files unchanged, including Puma's embedded MIT notice and nio4r's libev
notice. BSD alternatives are selected where the notices offer a choice.
The exact artifact URL and SHA-256 also bind the manually audited catalog
entries; changing a gem requires reviewing those entries again. These
records do not add permission to the repository's license policy.

When updating the application dependencies, resolve Gemfile with the
selected Ruby/Bundler version, update the source-gem pins and lock
together, and recheck the notice bindings. Both native extension builds
must use their matching new official image manifests. Keep the server and
its HTTP consumer unchanged when validating a packaging-only update.
