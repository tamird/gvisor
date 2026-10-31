set -euo pipefail
d=$RUNNER_TEMP/traffic
dest=$RUNNER_TEMP/installed-plugin
mkdir -p "$dest" "$d"
stage() { printf '%s\n' "$1" > "$d/stage"; }
stage fixture-image
test "$(cat images/basic/ubuntu/Dockerfile)" = 'FROM ubuntu:trusty'
docker pull ubuntu:trusty > "$d/image.log"
docker tag ubuntu:trusty gvisor.dev/images/basic/ubuntu
docker image inspect ubuntu:trusty --format '{{json .RepoDigests}}' >> "$d/image.log"
docker run --rm --entrypoint /bin/sh gvisor.dev/images/basic/ubuntu -c 'set -e; command -v nc; command -v awk; command -v grep' >> "$d/image.log"
export USE_BAZEL_VERSION
USE_BAZEL_VERSION=$(cat images/default/bazelversion)
baz() { bazelisk "--bazelrc=$RUNNER_TEMP/images.bazelrc" "$@"; }
stage binaries
baz build --config=x86_64 --config=plugin-tldk --build_event_json_file="$RUNNER_TEMP/plugin.bep" //:release //runsc:runsc-plugin-stack //runsc/cmd/sentry:gvisor_sentry_plugin_stack > "$RUNNER_TEMP/binaries.log" 2>&1
cp -LR bazel-bin/release/. "$dest/"
mkdir -p "$dest/gvisor-bin"
for label in //runsc:runsc-plugin-stack //runsc/cmd/sentry:gvisor_sentry_plugin_stack;do
 src=$(baz cquery --config=x86_64 --config=plugin-tldk --output=files "$label" 2>"$RUNNER_TEMP/paths.log")
 test -f "$src"
 name=runsc
 if test "$label" = //runsc/cmd/sentry:gvisor_sentry_plugin_stack;then name=gvisor-bin/gvisor_sentry_plugin_stack;fi
 install -m 0755 "$src" "$dest/$name"
 cmp "$src" "$dest/$name"
 sha256sum "$dest/$name" >> "$d/installed.sha256"
done
stage install
sudo "$dest/runsc" install --runtime=qualification-gnu-tldk --download-sidecars=NEVER -- --network=plugin --platform=systrap
sudo systemctl reload docker
for _ in {1..15};do docker info --format '{{json .Runtimes}}' | jq -e 'has("qualification-gnu-tldk")' >/dev/null && break;sleep 1;done
docker info --format '{{json .Runtimes}}' | jq -e 'has("qualification-gnu-tldk")' >/dev/null
sudo jq -c '.runtimes["qualification-gnu-tldk"]' /etc/docker/daemon.json > "$d/runtime.json"
jq -e --arg exe "$dest/runsc" '.path==$exe and (.runtimeArgs|index("--network=plugin")!=null)' "$d/runtime.json" >/dev/null
stage TestConnectToSelf
set +e
baz test --config=x86_64 --config=plugin-tldk --strategy=TestRunner=local --nocache_test_results --test_sharding_strategy=disabled --test_timeout=300 --test_env=GO_TEST_WRAP_TESTV=1 --test_arg=-test.v --test_arg=-runtime=qualification-gnu-tldk --test_filter='^TestConnectToSelf$' --test_output=all --build_event_json_file="$RUNNER_TEMP/e2e.bep" //test/e2e:integration_test > "$RUNNER_TEMP/guest.log" 2>&1
code=$?
set -e
echo "$code" > "$d/test-exit"
if test -f bazel-testlogs/test/e2e/integration_test/test.log;then cp bazel-testlogs/test/e2e/integration_test/test.log "$d/test.log";fi
exit "$code"
