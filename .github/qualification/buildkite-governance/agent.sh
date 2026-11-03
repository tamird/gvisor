# Keep the genuine hook's log selection. Intercept the hosted Buildkite API
# boundary; no raw test logs or annotations leave the runner.
buildkite-agent() {
  case "$1 $2" in
    "artifact download") return 0 ;;
    "artifact upload")
      test -f "$3" || return 81
      printf '%s\t%s\n' "$(sha256sum "$3" | cut -d' ' -f1)" "$(realpath --relative-to="$PROBE_BASE" "$3")" >> "$PROBE_OUT/upload.tsv"
      ;;
    "annotate --style") cat >/dev/null ;;
    *) return 82 ;;
  esac
}
