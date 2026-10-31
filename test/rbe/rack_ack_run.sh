set -euo pipefail
out="$RUNNER_TEMP/rack-results"
tools="$RUNNER_TEMP/label-tools"
mkdir -p "$out" "$tools"
for f in qualification_report.py unit_matrix.py label_result.py; do git show "$GITHUB_SHA:test/rbe/$f" >"$tools/$f"; done
product="49c59fcc0063fa0e98d2b383947e4c238be0f21a"
git checkout --quiet --detach "$product"
git update-index --refresh
test -z "$(git status --porcelain --untracked-files=no)"
label=release-20991231.7
git init --quiet --bare "$RUNNER_TEMP/tag-origin.git"
git -C "$RUNNER_TEMP/tag-origin.git" config receive.shallowUpdate true
git -c user.name=Qualifier -c user.email=qualifier@example.com tag -a "$label-staging" -m 'release fixture' "$product"
git push --quiet "$RUNNER_TEMP/tag-origin.git" "refs/tags/$label-staging"
git -c user.name=Qualifier -c user.email=qualifier@example.com tag -a dev-20981231.4 -m 'competing annotated tag' "$product"
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0="url.file://$RUNNER_TEMP/tag-origin.git.insteadOf"
export GIT_CONFIG_VALUE_0=https://github.com/google/gvisor.git
export BUILDKITE_TAG="$label-staging" RELEASE_NIGHTLY=false
if BUILDKITE_TAG='release-bad-staging' ./.buildkite/scripts/release/tag.sh stage >"$out/bad.stdout" 2>"$out/bad.stderr"; then
  echo "invalid staging label accepted" >&2; exit 1
fi
test ! -s "$out/bad.stdout"
grep -F 'invalid release tag: release-bad' "$out/bad.stderr"
python3 - "$out/staging.sh" <<'PY'
import sys, yaml
pipeline = yaml.safe_load(open(".buildkite/release.yaml"))
step = next(s for s in pipeline["steps"] if s.get("label") == ":ship: Release")
commands = step["commands"]
assert "tag.sh stage" in commands[0] and commands[1] == "make artifacts-python" and commands[2].startswith("make release ")
open(sys.argv[1], "w").write("\n".join(commands[:3]).replace("$$", "$") + "\n")
PY
# Exercise the actual stage script and all four Make entries. Only publication,
# signing and package staging are dry here; the real selected releases run below.
make() {
  local name
  case "$*" in
    *artifacts/x86_64*) name=x86_64;;
    *artifacts/aarch64*) name=aarch64;;
    *artifacts-python*) name=python;;
    *RELEASE_NIGHTLY*) name=sign;;
    *) return 1;;
  esac
  printf '%s\0' "$@" >"$out/$mode-$name.argv"
  printf '%s\n' "${BAZEL_OPTIONS:-}" >"$out/$mode-$name.env"
  /usr/bin/make --no-print-directory -n BAZEL=bazel DOCKER_BUILD=false "$@" >"$out/$mode-$name.make"
}
(
  mode=release
  source "$out/staging.sh"
  test "$tag" = "$label"
)
(
  mode=nightly
  unset BUILDKITE_TAG BAZEL_OPTIONS
  source "$out/staging.sh"
  test -z "$tag"
)
unset GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0
git describe --always --tags --exclude 'release-*-staging' --abbrev=12 --dirty >"$out/dev-label"
test "$(cat "$out/dev-label")" = dev-20981231.4
python3 - "$out" "$label" <<'PY'
from pathlib import Path
import shlex, sys
out, tag = Path(sys.argv[1]), sys.argv[2]
for mode in ("release", "nightly"):
    for arch in ("x86_64", "aarch64"):
        p = out / f"{mode}-{arch}"
        argv = Path(str(p)+".argv").read_bytes().split(b"\0")[:2]
        argv = [x.decode() for x in argv]
        assert argv[1] == f"artifacts/{arch}", argv
        args = shlex.split(argv[0].removeprefix("BAZEL_OPTIONS="))
        assert f"--config={arch}" in args and "--compilation_mode=opt" in args
        assert ("--embed_label="+tag in args) == (mode=="release"), args
        assert ("--config=staged_release" in args) == (mode=="release"), args
        make = Path(str(p)+".make").read_text()
        builds = [l for l in make.splitlines() if 'bazel build ' in l and '//debian:' in l]
        assert len(builds) == 3, (mode, arch, builds)
        for line in builds:
            for arg in args:
                assert arg in line, (arch,arg,line)
        Path(str(p)+".flags").write_text("\n".join(args)+"\n")
    for name in ("python","sign"):
        env = (out/f"{mode}-{name}.env").read_text().split()
        assert ("--embed_label="+tag in env) == (mode=="release"), (mode,name,env)
    # The native-arch prerequisite of "release" can invoke Bazel from signing.
    assert '//debian:debian' in (out/f"{mode}-sign.make").read_text()
PY
labels=(//runsc:runsc //runsc/cmd/sentry:gvisor_sentry //runsc/checkpointgofer:checkpointgofer_binary //runsc/cmd/metricserver:runsc-metric-server //tools/bazeldefs:version)
for arm in release-x86_64 release-aarch64 nightly-x86_64; do
  d="$out/$arm"
  mkdir -p "$d"
  mapfile -t flags <"$out/$arm.flags"
  printf '%s\n' "${labels[@]}" >"$d/targets"
  git rev-parse HEAD >"$d/source"
  git cat-file commit HEAD >"$d/commit"
  git status --porcelain --untracked-files=no >"$d/before"
  date -u +%FT%T.%3NZ >"$d/start"
  output_base=$(bazel --bazelrc="$rc" info output_base)
  result=0
  args=(build "${flags[@]}" --jobs=8 --strategy=GoStdlib=linux-sandbox --strategy=GoStdlibList=linux-sandbox --strategy=GoStandardLibraryAnalysis=linux-sandbox --strategy=GoStaticAnalysis=linux-sandbox '--modify_execution_info=^Go(StandardLibrary|Static)Analysis$=-no-sandbox,^Go(Stdlib(List)?|StandardLibraryAnalysis)$=+no-remote-cache' --noremote_upload_local_results --remote_download_outputs=toplevel --nobuild_event_json_file_path_conversion "--build_event_json_file=$RUNNER_TEMP/$arm.bep" "${labels[@]}")
  printf '%s\n' "${args[@]}" >"$d/arguments"
  bazel --bazelrc="$rc" "${args[@]}" >"$RUNNER_TEMP/$arm.log" 2>&1 || result=$?
  tail -35 "$RUNNER_TEMP/$arm.log"
  tail -90 "$RUNNER_TEMP/$arm.log" > "$d/build-tail"
  date -u +%FT%T.%3NZ >"$d/finish"; echo "$result" >"$d/exit"
  git status --porcelain --untracked-files=no >"$d/after"
  git rev-parse HEAD >"$d/head"
  git rev-parse "$product^{tree}" >"$d/tree"
  mkdir -p "$d/original"
  capture=0; python3 -B "$tools/qualification_report.py" capture --events "$RUNNER_TEMP/$arm.bep" --output "$d/events.jsonl" --output-root "$output_base" --report-dir "$d/original" || capture=$?
  echo "$capture" >"$d/capture-exit"
  python3 -B "$tools/label_result.py" context "$d" "$product"
  verify=0; python3 -B "$tools/qualification_report.py" verdict --events "$d/events.jsonl" --context "$d/context.json" --expected-targets "$d/targets" --output "$d/verdict.json" || verify=$?
  echo "$verify" >"$d/verifier-exit"
  if [[ "$result" == 0 ]]; then
    runner=(); [[ "$arm" == *-aarch64 ]] && runner=(qemu-aarch64)
    "${runner[@]}" bazel-bin/runsc/runsc_/runsc --version >"$d/cli" 2>"$d/cli.stderr"
    cp bazel-bin/tools/bazeldefs/version.txt "$d/package-version"
    for pathname in runsc/cmd/sentry/gvisor_sentry_/gvisor_sentry runsc/checkpointgofer/checkpointgofer_binary_/checkpointgofer_binary runsc/cmd/metricserver/runsc-metric-server_/runsc-metric-server; do
      exe=${pathname##*/}
      if [[ "$exe" == runsc-metric-server ]]; then
        wrong=0
        GVISOR_ENFORCE_RELEASE=deliberately-mismatched "${runner[@]}" "bazel-bin/$pathname" >"$d/$exe.wrong" 2>&1 || wrong=$?
        echo "$wrong" >"$d/$exe.exit"
      else
        "${runner[@]}" "bazel-bin/$pathname" --version >"$d/$exe.version" 2>&1
      fi
    done
  fi
done
python3 - "$out" "$product" "$label" <<'PY'
import json, sys
from pathlib import Path
out,product,label=Path(sys.argv[1]),sys.argv[2],sys.argv[3]
errors=[]; results={}
for name in ("release-x86_64","release-aarch64","nightly-x86_64"):
    d=out/name
    try:
        v=json.loads((d/"verdict.json").read_text())
        assert not v["errors"] and not v["missing"] and v["native_exit"] == 0 and v["source"]==product, v
        assert (d/"verifier-exit").read_text().strip()=="0" and len(v["expected_labels"])==5
        expected=label if name.startswith("release-") else "dev-20981231.4"
        assert (d/"cli").read_text().splitlines()[0] == "runsc version "+expected
        assert (d/"package-version").read_bytes() == (expected.split("-",1)[1]+"\n").encode()
        sidecars={}
        for exe in ("gvisor_sentry","checkpointgofer_binary","runsc-metric-server"):
            if exe=="runsc-metric-server":
                text=(d/(exe+".wrong")).read_text()
                assert int((d/(exe+".exit")).read_text()) != 0 and f'this binary\'s build label "{expected}"' in text and 'expected release "deliberately-mismatched"' in text, (exe,text[:1400])
            else:
                assert (d/(exe+".version")).read_text().splitlines()[0]=="runsc version "+expected, exe
            sidecars[exe]=expected
        results[name]=dict(source=product,version=expected,sidecars=sidecars,package=(d/"package-version").read_text().strip())
    except (AssertionError,KeyError,IndexError,ValueError,OSError) as exc:
        errors.append(f"{name}: {exc}")
report=dict(status="failed" if errors else "verified",results=results,errors=errors)
(out/"verdict.json").write_text(json.dumps(report,indent=2)+"\n")
print(json.dumps(report,indent=2)); sys.exit(bool(errors))
PY
