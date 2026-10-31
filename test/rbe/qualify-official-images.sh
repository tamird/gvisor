set -euo pipefail
name=$1
native=$2
case "$name" in
  ollama) product=1c5d0f089116d91c34262a4b63ebfc9fa7653e91; image=gpu/ollama/client; stem=client; package=//images/gpu/ollama/client; fixture=ollama_client_test; expected=TestOllamaClientImage ;;
  profile) product=6dd2bf31bde7cea6e0006856b634e4df64f5c3f2; image=benchmarks/profile-helper; stem=profile_helper; package=//images/benchmarks/profile-helper; fixture=profile_helper_test; expected=TestProfileHelperImage ;;
esac
case "$native" in x86_64) cpu=amd64 ;; aarch64) cpu=arm64 ;; esac
receipt="$RUNNER_TEMP/images-results/$name-$native"
mkdir -p "$receipt"
exec > >(tee "$receipt/coordinator.log") 2>&1
bz() { bazel --bazelrc="$RUNNER_TEMP/images.bazelrc" "$@"; }
git checkout --quiet --detach "$product"
test "$(git rev-parse HEAD)" = "$product"
printf '%s\n' "$product" > "$receipt/product"
goal="rebuild-${image//\//_}"
make -s -f tools/images.mk list-all-images ARCH="$native" | grep -Fx "${image//\//_}"
make -f tools/images.mk "$goal" ARCH="$native" DOCKER_BUILD=false STARTUP_OPTIONS="--bazelrc=$RUNNER_TEMP/images.bazelrc"
image_tag="gvisor.dev/images/$image:latest"
test "$(docker image inspect -f '{{.Architecture}}' "$image_tag")" = "$cpu"
docker image inspect "$image_tag" --format '{{json .Config}}' > "$receipt/config.json"
# The manifest is a declared default output alongside the config; use cquery to locate both.
bz build --config="$native" "$package:${stem}_image" "$package:${stem}_archive"
bz cquery --config="$native" --output=files "$package:${stem}_image" > "$receipt/manifest-files"
python3 - "$receipt" "$name" "$image" "$native" "$cpu" "$GITHUB_SHA" <<'PY'
import json,pathlib,subprocess,sys
r=pathlib.Path(sys.argv[1]);name,image,arch,cpu,carrier=sys.argv[2:]
rows=[json.loads(pathlib.Path(p).read_bytes()) for p in (r/'manifest-files').read_text().splitlines()]
manifest,= (v for v in rows if 'layers' in v)
config,=(v for v in rows if 'rootfs' in v)
assert config['architecture']==cpu,config
wanted=['/usr/bin/httpclient'] if name=='ollama' else ['/bin/bash']
assert config['config']['Cmd']==wanted,(wanted,config['config'])
assert len(manifest['layers'])==2,manifest
baseline=json.loads(subprocess.check_output(['git','show',f'{carrier}:test/rbe/shipped-images.json']))[image+'_'+arch]
total=sum(l['size'] for l in manifest['layers'])
receipt={'architecture':cpu,'shipped_manifest':baseline['manifest'],'shipped_compressed_bytes':baseline['total'],'candidate_compressed_bytes':total,'delta':total-baseline['total'],'shipped_layers':baseline['layers'],'candidate_layers':manifest['layers'],'cmd':wanted}
(r/'sizes.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt))
PY
if [[ "$name" == profile ]]; then
  docker run --rm --platform="linux/$cpu" "$image_tag" dpkg-query -W -f='${Package} ${Version}\n' coreutils libcrypt1 | tee "$receipt/installed-ubuntu-packages"
  grep -E '^coreutils ' "$receipt/installed-ubuntu-packages"
else
  docker run --rm --platform="linux/$cpu" "$image_tag" /bin/sh -ec 'test "$(stat -c %a /tmp)" = 1777; grep -c BEGIN.CERTIFICATE /etc/ssl/certs/ca-certificates.crt' | tee "$receipt/certificates"
fi
git checkout --quiet --detach "$GITHUB_SHA"
bz test --strategy=TestRunner=local --nocache_test_results --test_output=errors --test_arg=-test.v --test_timeout=240 --run_under="$RUNNER_TEMP/privileged-fixture" "//test/rbe:$fixture"
log="bazel-testlogs/test/rbe/$fixture/test.log"
cp "$log" "$receipt/test.log"
cp "bazel-testlogs/test/rbe/$fixture/test.xml" "$receipt/test.xml"
grep -F -- "--- PASS: $expected " "$receipt/test.log"
if [[ "$name" == ollama ]]; then grep -F -- '--- PASS: TestOllamaClientImage/system_certificates ' "$receipt/test.log"; fi
! grep -E -- '--- (FAIL|SKIP): ' "$receipt/test.log"
echo passed > "$receipt/verdict"
