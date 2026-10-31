set -euo pipefail
cd "$GITHUB_WORKSPACE"
arch=$1
r="$RUNNER_TEMP/results/$arch"
mkdir -p "$r"
exec > >(tee "$r/coordinator.log") 2>&1
bz() { bazel --bazelrc="$RUNNER_TEMP/images.bazelrc" "$@"; }
test "$(git rev-parse HEAD)" = "$PRODUCT"
git rev-parse HEAD > "$r/source"
case "$arch" in x86_64) cpu=amd64; machine='Advanced Micro Devices X86-64';; aarch64) cpu=arm64; machine=AArch64;; esac
target=//images/basic:integrationtest_context
bz build --config="$arch" "$target"
ctx=$(bz cquery --config="$arch" --output=files "$target")
t=$(mktemp -d "$RUNNER_TEMP/c-context.XXXXXX")
trap 'rm -rf "$t"' EXIT
tar -xf "$ctx" -C "$t"
folder="$t/images/basic/integrationtest"
helpers=(host_connect host_fd link_test tcp_loopback tcp_server tcp_stress_server test_copy_up test_rewinddir test_sticky)
test "$(find "$folder" -type f | wc -l)" -eq 13
for name in "${helpers[@]}"; do
  test -x "$folder/$name"
  readelf -h "$folder/$name" | grep -F "Machine:" | grep -F "$machine" >> "$r/elf"
  if readelf -l "$folder/$name" | grep -F INTERP; then exit 1; fi
  sha256sum "$folder/$name" | sed "s|$folder/||" >> "$r/helpers"
done
for file in Dockerfile copy_up_testfile.txt ping4.sh ping6.sh; do test -f "$folder/$file"; done
test -x "$folder/ping4.sh"
test -x "$folder/ping6.sh"
sha256sum "$ctx" >"$r/archive"
export MAKEFLAGS=DOCKER_BUILD=false
make -s rebuild-basic_integrationtest ARCH="$arch" DOCKER_BUILD=false STARTUP_OPTIONS="--bazelrc=$RUNNER_TEMP/images.bazelrc"
tag=gvisor.dev/images/basic/integrationtest:latest
docker image inspect "$tag" --format '{{.Architecture}} {{.Config.WorkingDir}} {{json .Config.Cmd}} {{.Size}}' | tee "$r/image"
grep -F "$cpu /root " "$r/image"
docker run --runtime=runc --platform=linux/"$cpu" --rm "$tag" bash -ec '
  id nonroot; test "$(id -u nonroot):$(id -g nonroot)" = 1338:1337
  cmp host_connect /home/nonroot/host_connect
  for program in bash stat unshare sysctl ip ping iptables; do command -v "$program"; done
  test -x /bin/ping6
  if command -v gcc || command -v make; then exit 1; fi
  test "$(dpkg-query -W -f="\${db:Status-Status}" build-essential 2>/dev/null || true)" != installed
  dpkg-query -W iproute2 iputils-ping iptables
  ./test_rewinddir; ./test_copy_up
' > "$r/runtime"
docker run --runtime=runc --platform=linux/"$cpu" --rm --user=nonroot "$tag" bash -ec 'test -x /home/nonroot/host_connect; stat -c "%a %u %g" /home/nonroot' >> "$r/runtime"
echo passed >"$r/verdict"
