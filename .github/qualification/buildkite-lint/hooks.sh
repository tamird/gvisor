# Privileged agent maintenance is outside this qualification on a disposable
# Linux runner. Leave the actual installer, Make, lint and Bazel unchanged.
sudo() {
  case "$1" in
    -E|rm|sysctl) return 0 ;;
    *) return 85 ;;
  esac
}
buildkite-agent() { [[ "$1" == artifact && "$2" == download ]]; }
docker() {
  if [[ "$#" == 2 && "$1" == ps && "$2" == '--format={{.ID}}/{{.Names}}' ]]; then
    echo inventory >> "$PROBE_OUT/hook-docker"
    return 0
  fi
  echo rejected >> "$PROBE_OUT/hook-docker"
  return 86
}
# Functions are deliberately not exported: subprocesses of Make see the
# fail-closed Docker executable and the real installed Bazel in their PATH.
case "$1" in
  pre)
    source .buildkite/hooks/pre-command
    printf '%s\n' "$BUILDKITE_BAZEL_DIRECTORY" "$(command -v bazel)" "$PATH" > "$PROBE_OUT/environment"
    ;;
  post) source .buildkite/hooks/post-command ;;
esac
