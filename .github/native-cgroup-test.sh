#!/bin/bash
# Run the existing Bazel test under its local tag on an unprivileged ARM host.
set -euo pipefail
[[ $(uname -m) == aarch64 && $(id -u) != 0 ]]
[[ $(git rev-parse HEAD) == "$QUALIFICATION_COMMIT" ]]
[[ $QUALIFICATION_TARGETS == //runsc/cgroup:cgroup_test_arm64 ]]
if [[ $(stat -f -c %T /sys/fs/cgroup) == cgroup2fs ]]; then cgroups=true; else cgroups=false; fi
compile_routing=$(python3 test/rbe/native-cgroup-routing.py)
bazel test --config=buildbuddy_remote_executor --config=x86_64 --compilation_mode=opt \
  --host_platform=@io_bazel_rules_go//go/toolchain:linux_amd64_cgo \
  --extra_execution_platforms=//test/rbe:native_amd64,//test/rbe:native_arm64 \
  --remote_default_exec_properties=OSFamily=linux --remote_default_exec_properties=Arch=amd64 \
  --remote_default_exec_properties=workload-isolation-type=firecracker \
  --remote_default_exec_properties=container-image=docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a \
  "$compile_routing" --jobs=400 --spawn_strategy=remote --noremote_local_fallback --strategy=TestRunner=local \
  --noremote_upload_local_results --remote_download_outputs=all \
  --test_tag_filters=-nogo,-requires-kvm --build_tag_filters=-network_plugins \
  --test_env=CGROUPV2="$cgroups" --test_env=GO_TEST_WRAP=1 --test_env=GO_TEST_WRAP_TESTV=1 \
  --test_output=errors -- "$QUALIFICATION_TARGETS"
