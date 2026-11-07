#!/bin/bash
# The immutable Jammy client has no installed packages beyond its base image.
set -eu -o pipefail
for tool in git bash python3 patch zip unzip cc gcc clang as ld ar make docker; do
    printf '%s\t%s\n' "$tool" "$(type -P "$tool" || :)" >> /audit/client-tools
done
test ! -e .git
test ! -L .git
if type -P git; then exit 1; fi
echo absent > /audit/git-metadata
bazel --version > /audit/client-version
test "$(cat /audit/client-version)" = 'bazel 9.3.1'

# Only the tracer uses its private libunwind. The observed programs inherit
# the ordinary image's library search path and Docker's default permissions.
strace=(/lib64/ld-linux-x86-64.so.2 --library-path /observer/usr/lib/x86_64-linux-gnu
        /observer/usr/bin/strace --seccomp-bpf -f -qq -z -s 0 -e trace=execve,execveat,clone,clone3,fork,vfork)
trace=()
if "${strace[@]}" -o /audit/probe.trace /bin/true 2>/audit/probe.error &&
   grep -Fq 'execve("/bin/true"' /audit/probe.trace; then
    trace=("${strace[@]}")
fi
failed=0
for cpu in arm64 amd64; do
    config=x86_64
    [[ "$cpu" == arm64 ]] && config=aarch64
    output=/audit/$cpu
    observe=()
    if ((${#trace[@]})); then observe=("${trace[@]}" -o "$output/exec.trace"); fi
    rc=0
    "${observe[@]}" /usr/local/bin/bazel --batch --nosystem_rc --nohome_rc build \
        "--config=$config" --config=release_version "--embed_label=$RELEASE_LABEL" \
        --jobs=4 --remote_cache= --remote_executor= --disk_cache= \
        --noshow_progress --color=no --curses=no --verbose_failures //:release > "$output/build.log" 2>&1 || rc=$?
    echo "$rc" > "$output/build.exit"
    if ((rc)); then failed=1; continue; fi
    # Copy outside the observed command, before bazel-bin changes target.
    cp -RL bazel-bin/release "$output/files" || failed=1
done
exit "$failed"
