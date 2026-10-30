#!/bin/bash
set -euo pipefail
out="$RUNNER_TEMP/publication"
git checkout --quiet --detach "$CURRENT_PRODUCT"
export USE_BAZEL_VERSION
USE_BAZEL_VERSION=$(cat images/default/bazelversion)
rc="$RUNNER_TEMP/current-release.bazelrc"
for lane in gnu scratch arm64; do
  data="$RUNNER_TEMP/release-$lane"
  mkdir -p "$data"
  flags=(--spawn_strategy=local -c opt --//sandboxexec/sandbox/python:release_version=2026.10.29.2)
  if [[ $lane != gnu ]]; then
    arch=amd64
    if [[ $lane == arm64 ]]; then arch=arm64; fi
    flags+=(--spawn_strategy=remote "--host_platform=//tools/bazeldefs:linux_$arch" "--extra_execution_platforms=//tools/bazeldefs:linux_$arch" "--platforms=//tools/bazeldefs:linux_$arch")
    flags+=(--config=static-python --remote_executor=grpcs://remote.buildbuddy.io --remote_cache=grpcs://remote.buildbuddy.io
      --remote_default_exec_properties=OSFamily=linux "--remote_default_exec_properties=Arch=$arch"
      --remote_default_exec_properties=workload-isolation-type=oci
      --remote_default_exec_properties=container-image=docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef
      --strategy=PythonDistribution=remote --remote_download_outputs=toplevel)
  fi
  status=0
  bazelisk --bazelrc="$rc" build --lockfile_mode=update --bes_backend=grpcs://remote.buildbuddy.io --bes_results_url=https://app.buildbuddy.io/invocation/ --nobuild_event_json_file_path_conversion --build_event_json_file="$data/events" --execution_log_compact_file="$data/execution.binpb" "${flags[@]}" //sandboxexec/sandbox/python:dist >"$data/private.log" 2>&1 || status=$?
  echo "$status" >"$out/current-$lane.exit"
  python3 - "$data" "$lane" "$out" "$status" <<'PYDIGEST'
from pathlib import Path
import json,re,sys,hashlib
d, lane, out, status=sys.argv[1:]
d=Path(d); out=Path(out); status=int(status)
events=[json.loads(l) for l in (d/"events").read_text().splitlines()] if (d/"events").exists() else []
expected={"whl":"7e9e8d37be9b4d66a18fe5c3d9216767e51e25c9581a4d00a2dab6d232f43a29","tar.gz":"e82b0f28d7618677b5f4cc408017a366e79b61b09f5ac572e14fdee83cf851ad"}
found={}
if not status:
    for ext in expected:
        files=list(Path("bazel-bin/sandboxexec/sandbox/python/dist").glob("*."+ext))
        if len(files)==1: found[ext]=hashlib.sha256(files[0].read_bytes()).hexdigest()
lines=(d/"private.log").read_text(errors="replace").splitlines()
diagnostics=[re.sub(r"https?://[^\s),;]+","[URL]",l)[:550] for l in lines if re.search(r"error|failed|cannot|no matching",l,re.I) and not re.search(r"header|secret|token|credential|api[-._]key|authorization",l,re.I)]
(out/(lane+"-archives.json")).write_text(json.dumps({"native_exit":status,"digests":found,"original_digests_match":found==expected,"invocation":next((e["started"].get("uuid") for e in events if "started" in e),None),"diagnostics":diagnostics[-10:]},indent=2)+"\n")
PYDIGEST
done
for lane in gnu scratch arm64; do test "$(cat "$out/current-$lane.exit")" = 0 || exit 1; done

python3 - "$out" <<'PYRESULT'
import json,sys
from pathlib import Path
for lane in ("gnu","scratch","arm64"):
    assert json.loads((Path(sys.argv[1])/(lane+"-archives.json")).read_text())["original_digests_match"],lane
PYRESULT
