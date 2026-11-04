set -euo pipefail
actual=$(git rev-parse HEAD)
test "$actual" = 683c2687297d36a04c02cae21a8be4e135a7ff16
test "$(uname -m)" = x86_64
export WORK=$(mktemp -d)
trap 'rm -rf -- "$WORK"' EXIT
public=${RUNNER_TEMP:?}/public/gazelle.json
export public
printf '{"status":"INCOMPLETE","phase":"bootstrap","source":"%s"}\n' "$actual" > "$public"
reader=05cf00d9d26a5beb8a9af8011375644b8b37a086
git fetch -q --depth=1 https://github.com/tamird/gvisor.git "$reader"
for module in qualification_report unit_matrix; do git show "$reader:test/rbe/$module.py" > "$WORK/$module.py"; done
helper=38745c527784376e0b5d915fe5ebd85e1d1385fd
git fetch -q --depth=1 https://github.com/tamird/gvisor.git "$helper"
git show "$helper:test/rbe/credential_artifacts.py" > "$WORK/credential_artifacts.py"
mkdir -m 700 "$WORK/auth" "$WORK/platform"
PYTHONPATH="$WORK" python3 -B -c 'from pathlib import Path; import os; from credential_artifacts import prepare; prepare(Path(os.environ["WORK"])/"auth")'
unset BUILDBUDDY_API_KEY
printf '%s\n' 'module(name="gazelle_platform")' > "$WORK/platform/MODULE.bazel"
cat > "$WORK/platform/BUILD" <<'BUILD'
platform(
    name = "ubuntu",
    constraint_values = ["@platforms//cpu:x86_64", "@platforms//os:linux"],
    exec_properties = {
        "Arch": "amd64",
        "OSFamily": "linux",
        "workload-isolation-type": "oci",
        "container-image": "docker://mirror.gcr.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
    },
    visibility = ["//visibility:public"],
)
BUILD
cat > "$WORK/native.bazelrc" <<RC
common --credential_helper=remote.buildbuddy.io=$WORK/auth/helper
build --remote_executor=grpcs://remote.buildbuddy.io
build --remote_cache=grpcs://remote.buildbuddy.io
build --bes_backend=grpcs://remote.buildbuddy.io
build --bes_results_url=https://app.buildbuddy.io/invocation/
build --spawn_strategy=remote --noremote_local_fallback --jobs=8
build --build_event_json_file=$WORK/build.events
cquery --build_event_json_file=$WORK/cquery.events
build --inject_repository=gazelle_platform=$WORK/platform
build --host_platform=@gazelle_platform//:ubuntu --extra_execution_platforms=@gazelle_platform//:ubuntu
build --platforms=//tools/bazeldefs:linux_amd64
RC
export DOCKER_BUILD=false DOCKER_HOST="unix://$WORK/no-docker.sock"
sed -n '/^install_bazelisk() {$/,$p' .buildkite/hooks/pre-command > "$WORK/bootstrap.sh"
source "$WORK/bootstrap.sh"
test "$(bazel --version)" = "bazel $(cat .bazelversion)"
export PYTHONPATH="$WORK" PYTHONDONTWRITEBYTECODE=1
python3 - <<'PY'
from datetime import datetime, timezone
import json, os, re, shlex, subprocess
from pathlib import Path
import qualification_report as reports

work = Path(os.environ["WORK"])
receipt = Path(os.environ["public"])
source = "683c2687297d36a04c02cae21a8be4e135a7ff16"
stale = "102528b84cae86055fdd7252732dabb073adb9d2"
target = "//:gazelle_check"
report = {"source": source, "status": "INCOMPLETE", "expected": ["first-pass", "pipeline", "stale"], "results": {}, "missing": [], "skips": []}
def now():
    return datetime.now(timezone.utc).isoformat()
def git(*args):
    return subprocess.check_output(["git", *args])
def save():
    report["missing"] = [job for job in report["expected"] if job not in report["results"]]
    report["verdict_available_at"] = now()
    receipt.write_text(json.dumps(report, sort_keys=True) + "\n")
def entry(pipeline):
    text = Path(".buildkite", pipeline + ".yaml").read_text()
    step, = [s for s in re.split(r"(?m)^  - ", text) if 'label: ":building_construction: Gazelle BUILD file check"' in s]
    command, = re.findall(r"(?m)^    command: (.+)$", step)
    assert shlex.split(command) == ["make", "gazelle-check"]
    assert '    env:\n      DOCKER_BUILD: "false"\n' in step and '<<: *source_test' in step
    return command
def run(name, cmd, commit):
    git("switch", "-q", "--detach", commit)
    phase = work / name
    phase.mkdir()
    tag = "--tool_tag=buildkite-gazelle-" + name
    args = shlex.split(cmd) + [f"STARTUP_OPTIONS=--bazelrc={work}/native.bazelrc", f"BAZEL_REMOTE_CACHE={tag}"]
    dry = subprocess.check_output([args[0], "-n", *args[1:]], text=True)
    bazel, = [shlex.split(line) for line in dry.splitlines() if target in line and " run " in line]
    assert bazel[0] == "bazel" and bazel[-1] == target and tag in bazel
    (phase / "args").write_text(json.dumps(bazel[1:]))
    (phase / "commit").write_bytes(git("cat-file", "commit", commit))
    (phase / "before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    events = work / "build.events"
    events.unlink(missing_ok=True)
    with (phase / "stdout.log").open("wb") as stdout, (phase / "stderr.log").open("wb") as stderr:
        rc = subprocess.run(args, stdout=stdout, stderr=stderr).returncode
    finished = now()
    report["execution_finished_at"] = finished
    (phase / "head").write_bytes(git("rev-parse", "HEAD"))
    (phase / "after").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    (phase / "native").write_text(str(rc))
    (phase / "collection").write_text("0")
    context = dict(expected_source=commit, commit_file="commit", source_before="before", source_after="after", final_head="head", executed_arguments="args", native_exit="native", capture_exit="collection", execution_finished_at=finished, command_kind="command")
    (phase / "context").write_text(json.dumps(context))
    verdict = reports.command_verdict(phase / "context")
    row = {"status": "INCOMPLETE", "source": commit, "make_exit": rc, "target": target, "command_status": verdict["status"], "execution_finished_at": finished, "source_errors": verdict["errors"]}
    report["results"][name] = row
    save()
    assert not verdict["errors"], verdict["errors"]
    assert events.is_file(), "missing Bazel run BEP"
    records = [json.loads(line) for line in events.read_text().splitlines()]
    row["run_observed"] = any(e.get("started", {}).get("command") == "run" for e in records)
    row["target_built"] = any(e.get("id", {}).get("targetCompleted", {}).get("label") == target and e.get("completed", {}).get("success") for e in records)
    output = (phase / "stdout.log").read_bytes() + (phase / "stderr.log").read_bytes()
    row["stale_dependency_reported"] = b"pkg/linewriter/BUILD" in output and b'+    deps = ["//pkg/sync"],' in output
    missed = name == "stale"
    row["status"] = "PASS" if row["run_observed"] and row["target_built"] and rc == (2 if missed else 0) and row["stale_dependency_reported"] == missed else "FAIL"
    row["verdict_available_at"] = now()
    save()
    if row["status"] != "PASS":
        print("Unexpected Gazelle result:", name, rc, flush=True)
        raise SystemExit(rc or 1)
save()
for name in ["first-pass", "pipeline"]:
    run(name, entry(name), source)
run("stale", entry("pipeline"), stale)
report["status"] = "PASS"
save()
PY
