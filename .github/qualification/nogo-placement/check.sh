#!/bin/bash
set -euo pipefail
tmp=$1
out=$tmp/placement
bzl=(bazel --bazelrc="$tmp/placement.bazelrc")
# Use the same lock seed and registry resolution as the original qualification.
cp test/rbe/scratch-release.lock MODULE.bazel.lock
"${bzl[@]}" mod graph --lockfile_mode=update --depth=1 --output=json >"$out/modules.json"
sha256sum MODULE.bazel.lock >"$out/lock.sha256"
opts=(--config=rbe --config=x86_64 --remote_accept_cached --jobs=32)
lib='//pkg/sentry/fsimpl/fsconfigfd:fsconfigfd_nogo //pkg/sentry/fsimpl/mountfd:mountfd_nogo //pkg/sentry/kernel/pipe:pipe_nogo //pkg/bits:bits_nogo'
pattern='.*(pkg/abi/linux/linux|pkg/bits/bits|pkg/sentry/fsimpl/fsconfigfd/fsconfigfd|pkg/sentry/fsimpl/mountfd/mountfd|pkg/sentry/kernel/pipe/pipe)[.]findings$'
q="outputs('$pattern', mnemonic('GoStaticAnalysis', deps(set($lib))))"
"${bzl[@]}" cquery "${opts[@]}" //tools/bazeldefs:rbe_linux_amd64_abi_nogo --output=starlark --starlark:expr='json.encode(target.rule.attr.exec_properties)' > "$out/platform.json"
"${bzl[@]}" aquery "${opts[@]}" --include_aspects --output=jsonproto "$q" > "$out/configured.json"
"${bzl[@]}" aquery "${opts[@]}" --include_aspects --output=jsonproto "outputs('.*/pkg/abi/linux/linux[.]findings$', mnemonic('GoStaticAnalysis', deps(//pkg/bits:bits_nogo)))" > "$out/probe-deps.json"
python3 .github/qualification/nogo-placement/verify.py configured "$out"
# Only the preflight witness may execute here, never the ABI analyzer.
"${bzl[@]}" build "${opts[@]}" --execution_log_json_file="$out/probe.spawns" --build_event_json_file="$out/probe.bep" //pkg/bits:bits_nogo
