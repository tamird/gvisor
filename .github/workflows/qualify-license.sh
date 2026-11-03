set -euo pipefail
actual=$(git rev-parse HEAD)
test "$actual" = 896acc45666c609ab05f92160f322f976a86cea1
test "$(uname -m)" = x86_64
export WORK=$(mktemp -d)
trap 'rm -rf -- "$WORK"' EXIT
public=${RUNNER_TEMP:?}/public/license.json
export public
printf '{"status":"INCOMPLETE","phase":"bootstrap","source":"%s"}\n' "$actual" > "$public"
reader=05cf00d9d26a5beb8a9af8011375644b8b37a086
git fetch -q --depth=1 https://github.com/tamird/gvisor.git "$reader"
for module in qualify_licenses qualification_report unit_matrix; do git show "$reader:test/rbe/$module.py" > "$WORK/$module.py"; done
helper=38745c527784376e0b5d915fe5ebd85e1d1385fd
git fetch -q --depth=1 https://github.com/tamird/gvisor.git "$helper"
git show "$helper:test/rbe/credential_artifacts.py" > "$WORK/credential_artifacts.py"
mkdir -m 700 "$WORK/auth" "$WORK/platform"
PYTHONPATH="$WORK" python3 -B -c 'from pathlib import Path; import os; from credential_artifacts import prepare; prepare(Path(os.environ["WORK"])/"auth")'
unset BUILDBUDDY_API_KEY
printf '%s\n' 'module(name="catalog_platform")' > "$WORK/platform/MODULE.bazel"
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
build --spawn_strategy=remote --noremote_local_fallback --noremote_accept_cached --jobs=8
build --build_event_json_file=$WORK/build.events
cquery --build_event_json_file=$WORK/cquery.events
build --inject_repository=catalog_platform=$WORK/platform
build --host_platform=@catalog_platform//:ubuntu --extra_execution_platforms=@catalog_platform//:ubuntu
build --platforms=//tools/bazeldefs:linux_amd64
RC
export DOCKER_BUILD=false DOCKER_HOST="unix://$WORK/no-docker.sock"
sed -n '/^install_bazelisk() {$/,$p' .buildkite/hooks/pre-command > "$WORK/bootstrap.sh"
source "$WORK/bootstrap.sh"
test "$(bazel --version)" = "bazel $(cat .bazelversion)"
export PYTHONPATH="$WORK" PYTHONDONTWRITEBYTECODE=1
python3 - <<'PY'
from datetime import datetime, timezone
import hashlib, json, os, re, shlex, subprocess
from pathlib import Path
import qualify_licenses as licenses

source = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
work = Path(os.environ['WORK'])
receipt = Path(os.environ['public'])
phases = {}
result = {'status': 'INCOMPLETE', 'source': source, 'jobs': [], 'phases': phases}
def publish():
    result['verdict_available_at'] = datetime.now(timezone.utc).isoformat()
    receipt.write_text(json.dumps(result, sort_keys=True) + '\n')
for file in ('.buildkite/first-pass.yaml', '.buildkite/pipeline.yaml'):
    step, = (part for part in re.split(r'(?m)^  - ', Path(file).read_text()) if 'label: ":scales: Dependency license check"' in part)
    command, = re.findall(r'(?m)^    command: (.+)$', step)
    assert shlex.split(command) == ['make', 'license-check'] and '    env:\n      DOCKER_BUILD: "false"\n' in step
    result['jobs'].append({'file': file, 'command': command, 'DOCKER_BUILD': 'false'})
publish()
args = [*shlex.split(command), f"STARTUP_OPTIONS=--bazelrc={work}/native.bazelrc"]
clean = licenses.phase('make', args, work, source, None)
phases['clean'] = clean
result['execution_finished_at'] = datetime.now(timezone.utc).isoformat()
if clean['status'] != 'PASS':
    result['status'] = clean['status']
    publish()
    raise SystemExit(clean['native_exit'] or 1)
publish()
events = work / 'build.events'
with events.open() as stream:
    build_commands = []
    runners = {}
    for line in stream:
        event = json.loads(line)
        if 'started' in event:
            build_commands.append(event['started']['command'])
        for row in event.get('buildMetrics', {}).get('actionSummary', {}).get('runnerCount', []):
            runners[row['name']] = int(row['count'])
result['clean_build'] = {'commands': build_commands, 'runners': runners}
if build_commands != ['build'] or runners.get('remote', 0) == 0:
    publish()
    raise SystemExit('missing remote build action evidence')
catalog = Path('tools/licensecheck/dependencies.yaml')
original = catalog.read_bytes()
truncated, count = re.subn(rb'(?ms)^- dependency: abseil-cpp\n.*?(?=^- dependency: )', b'', original)
assert count == 1
try:
    catalog.write_bytes(truncated)
    probe = licenses.phase('negative', args, work, source, None)
    stderr = (work / 'negative' / 'stderr.log').read_bytes().splitlines()
    detected = stderr.count(b'missing entry for abseil-cpp') == 1
    phases['removed_abseil_cpp'] = {'native_exit': probe['native_exit'], 'detected': detected, 'catalog_sha256': hashlib.sha256(truncated).hexdigest()}
    result['execution_finished_at'] = datetime.now(timezone.utc).isoformat()
finally:
    catalog.write_bytes(original)
result['status'] = 'PASS' if detected and probe['native_exit'] == 2 else 'FAIL'
publish()
print('license-check', result['status'], clean['counts'], flush=True)
raise SystemExit(result['status'] != 'PASS')
PY
