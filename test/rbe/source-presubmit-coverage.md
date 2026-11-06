# Source PR presubmit coverage of the mixed RBE run

All six test actions ran remotely without cached results in one Bazel invocation. Four exercise logical owners from three source-PR rows. Two test the worker architecture. No complete presubmit row is established by this run. [Original receipt](https://github.com/tamird/gvisor/actions/runs/42232937931/artifacts/13510273674) · [Bazel invocation](https://app.buildbuddy.io/invocation/167a7eaf-bf6c-4391-ad0a-4bd571c43d00)

| Passed leaf | Corresponding public workload | Unverified part |
|---|---|---|
| `atomicbitops_test_amd64`, `atomicbitops_test_arm64` | AMD64 cgroup-v2 and ARM64 unit rows | Other owners, the effective cgroup environment and the public source revision |
| `uname_test_native_amd64`, `uname_test_runsc_systrap_shared_amd64` | AMD64 syscall row | Other modes/owners, kernel capabilities, public options and partitions |
| `posix_error_test_amd64`, `posix_error_test_arm64` | Architecture controls | `//test/util/...` is outside the upstream unit and syscall command roots |

The source is [upstream `f51a205a8`](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml). The [fork suite](https://github.com/tamird/gvisor/blob/9fd10bc0c/test/rbe/BUILD#L123-L133) descends from the October 5 common ancestor `b9961ddfa`. The compared Go test sources and `uname.cc` match across these revisions. The six-result receipt records label, status, strategy and cache use. It omits Bazel configuration checksums, effective test environments and per-case XML. The suffixes identify the requested architecture and syscall mode, and do not establish equality of the full configurations.

The primary pipeline has 62 eligible rows before optional gates, four conditional GPU rows and four conditional image rows. Together they expand to 242 Buildkite partitions when enabled. Ten rows have no branch or staged-input predicate. They cover AMD64 syscalls, overlay, host networking, software GSO, fsstress and the five language runtime suites. The 38 master-only rows and one staged-only consistency check are listed separately in the [machine-readable inventory](source-presubmit-rows.jsonl). That inventory also records all 12 command rows of the separate first-pass pipeline. Each line contains the original command or command list, predicate, agent selectors, parallelism and row environment. Pipeline-wide settings and hook behavior remain in the linked upstream source.

The [AMD64 cgroup-v2 unit comparison](source-unit-cgroup-v2.md) reconciles the first row against actual Bazel selections. Of 360 upstream runnable tests, the fork unit profile contains 319 with matching labels.

### Where to expand the graph

1. Reconcile the actual unit and syscall selections first. Current `make unit-tests` includes `examples/...`. The fork patterns omit that root, add `//test/runner/...` and `//test/util/...`, and exclude metric, coretag, tracereplay and trace test owners. For syscalls the two trees share the `test/syscalls/...` and `test/rtnetlink/...` roots. Upstream passes runtime, sidecar location, host kernel, `-Werror` and per-job partition variables. The primary AMD64 and ARM64 rows use 20 and 10 Buildkite partitions. The fork's optional 15-way label bucketing is a separate mechanism. Validate the union of actual owners, modes, shards and cases rather than equating bucket numbers. [Public Make commands](https://github.com/google/gvisor/blob/f51a205a8/Makefile#L264-L320) · [Fork unit patterns](https://github.com/tamird/gvisor/blob/9fd10bc0c/test/unit.targets) · [Buildkite partition variables](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/hooks/pre-command#L60-L64)
2. Assign runtime requirements to the test action while compiles use ordinary workers. The fork declares Linux AMD64/ARM64 execution platforms and test-only Firecracker properties for native, ptrace and systrap. Verify cgroup v1, usable KVM, and privileged ARM64 placement before counting their rows. GPU runtime rows additionally require the actual hardware and driver on the specified Ubuntu or COS workers. Image build checks alone establish no GPU runtime coverage. [Platform definitions](https://github.com/tamird/gvisor/blob/9fd10bc0c/tools/bazeldefs/platforms.bzl)
3. Verify non-test outputs and side effects separately. Compiling selected dependencies cannot replace the public build-only rows, Go export, release archives or source checks. Master-only kernel experiments and benchmark publication have separate predicates. The staged-only consistency check consumes an externally supplied archive and its installed binary.

### Existing evidence outside the six-target run

| Earlier observation | Boundary |
|---|---|
| [Mixed unit invocation](https://app.buildbuddy.io/invocation/4011e97c-3824-4e04-a971-64d512a449fb) reported 669 passing owners, of which 204 executed. | Earlier selection on a different source. Inspect the [configured action manifest](https://github.com/tamird/gvisor/actions/runs/37696988219/artifacts/11517082185) before reusing its owner list. |
| [Native shared memory](https://github.com/tamird/gvisor/actions/runs/41308244805/artifacts/13083684908) ran four cases uncached on each remote architecture. | Native ARM64 syscall subset. The ordinary upstream ARM64 row selects ptrace/systrap. |
| [Rootless AMD64 race release](https://github.com/tamird/gvisor/actions/runs/42230903371/artifacts/13510997881) ran a matched Sentry and a guest pipeline; all nine installed files matched their producers. | Native Actions host on compiler source `3dab3494e`; [ordinary control](https://github.com/tamird/gvisor/actions/runs/42226845457/artifacts/13507627714) was a separate run. |
| [KVM integration](https://app.buildbuddy.io/invocation/40933722-a6e9-46b7-860e-6e960852fcfc) ran all 17 selected owners. | The coordinator supplied `/dev/kvm`. A remote worker with nested KVM must be qualified separately. |
| [ARM64 64KiB guest](https://github.com/tamird/gvisor/actions/runs/38994184655/artifacts/12087797446) produced 1,296 logs identifying 65,536-byte pages and kernel `6.8.0-138-generic-64k`. | AMD64 worker with QEMU TCG. The full verdict was incomplete, with 32 all-skipped owners. This row is master-only in the current primary pipeline. |

### Primary pipeline rows

Each table preserves the upstream command and links to its YAML row. Fork lanes below are implementation starting points from the [qualifier](https://github.com/tamird/gvisor/blob/9fd10bc0c/test/rbe/qualify.sh). Only the four logical owners above have execution evidence from the six-target run. Expand a group to inspect its outstanding work.

<details><summary>Build and policy · 19 rows</summary>

| Upstream row and command | Fork lane or target | Remaining check |
|---|---|---|
| [Smoke tests (AMD64)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L85)<br>`make smoke-tests` | smoke · //:release_smoke_test | Boot the source-built CLI with its matching Sentry in a rootless namespace on this architecture. |
| [Smoke race tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L92)<br>`make smoke-race-tests` | smoke-race · //:release_smoke_race_test | Use the race pair and the upstream GLIBC_TUNABLES. The separate AMD64 release control ran on a native Actions host. |
| [Smoke tests (ARM64)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L110)<br>`make smoke-tests` | smoke · //:release_smoke_test | Boot the source-built CLI with its matching Sentry in a rootless namespace on this architecture. |
| [Compile runsc-plugin-stack (AMD64)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L118)<br>`make runsc-plugin-stack` | plugin-build · //runsc:runsc-plugin-stack-build | Build the actual optimized plugin target with its link configuration. |
| [Build runsc and pkg (AMD64)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L127)<br>`make build TARGETS=//pkg/...`<br>`make build OPTIONS='--build_tag_filters=-network_plugins' TARGETS='//runsc/...'` | presubmit-build · //pkg/... and //runsc/... | Retain both build-only roots and their different build filters, including non-test outputs. |
| [Build runsc and pkg (ARM64)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L137)<br>`make build TARGETS=//pkg/...`<br>`make build OPTIONS='--build_tag_filters=-network_plugins' TARGETS='//runsc/...'` | presubmit-build · //pkg/... and //runsc/... | Retain both build-only roots and their different build filters, including non-test outputs. |
| [Go branch](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L159)<br>`tools/go_branch.sh`<br>`git checkout go && git clean -xf .`<br>`go build ./...` | go-export · //tools/go_export:all_test | Compare the exported source tree, package list and requested GOOS/GOARCH. Build the resulting module as an external consumer. |
| [Netstack on Mac](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L170)<br>`tools/go_branch.sh`<br>`git checkout go && git clean -xf .`<br>`GOOS=darwin GOARCH=arm64 go build $$PACKAGES` | go-export · //tools/go_export:all_test | Compare the exported source tree, package list and requested GOOS/GOARCH. Build the resulting module as an external consumer. |
| [Netstack on Windows](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L180)<br>`tools/go_branch.sh`<br>`git checkout go && git clean -xf .`<br>`GOOS=windows GOARCH=amd64 go build $$PACKAGES` | go-export · //tools/go_export:all_test | Compare the exported source tree, package list and requested GOOS/GOARCH. Build the resulting module as an external consumer. |
| [Netstack on FreeBSD](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L190)<br>`tools/go_branch.sh`<br>`git checkout go && git clean -xf .`<br>`GOOS=freebsd GOARCH=amd64 go build $$PACKAGES` | go-export · //tools/go_export:all_test | Compare the exported source tree, package list and requested GOOS/GOARCH. Build the resulting module as an external consumer. |
| [Netstack on OpenBSD](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L200)<br>`tools/go_branch.sh`<br>`git checkout go && git clean -xf .`<br>`GOOS=openbsd GOARCH=amd64 go build $$PACKAGES` | go-export · //tools/go_export:all_test | Compare the exported source tree, package list and requested GOOS/GOARCH. Build the resulting module as an external consumer. |
| [Netstack on 32-bit Linux](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L210)<br>`tools/go_branch.sh`<br>`git checkout go && git clean -xf .`<br>`GOOS=linux GOARCH=mips go build $$PACKAGES` | go-export · //tools/go_export:all_test | Compare the exported source tree, package list and requested GOOS/GOARCH. Build the resulting module as an external consumer. |
| [Release tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L286)<br>`make BAZEL_OPTIONS=--config=x86_64 artifacts/x86_64`<br>`make BAZEL_OPTIONS=--config=aarch64 artifacts/aarch64`<br>`make release` | release-artifacts · //test/release:artifacts; release-repository · //test/release:repository_test | Verify both architecture artifacts and the public release layout; preserve release-test inputs and keep signing/publishing outside this source comparison. |
| [Lint](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L341)<br>`make lint` | lint · //tools/lint:lint_tests | Compare each current source-lint command and tool version to the fork suite. |
| [Lint (C++)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L347)<br>`make lint-cc` | lint-cc · --config=lint-cc | Verify the complete current C++ lint input selection and toolchain; this fork lane is build-only. |
| [Governance file sync check](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L353)<br>`make governance-check` | governance · //:governance-check | Run the generated-file comparison. |
| [Gazelle BUILD file check](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L359)<br>`make gazelle-check` | No matching fork lane identified | Current make gazelle-check runs //:gazelle_check. Add a declared verification target to the submitted graph. |
| [Dependency license check](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L365)<br>`make license-check` | license-check · make license-check DOCKER_BUILD=false | The fork lane still calls a coordinator command; provide a testable source/license verification action with the current catalog. |
| [Website tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L704)<br>`make website-build` | website · //website:artifact | Build the current public website artifact. |

</details>

<details><summary>Unit and syscall · 8 rows</summary>

| Upstream row and command | Fork lane or target | Remaining check |
|---|---|---|
| [Nogo tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L373)<br>`make nogo-tests` | nogo · //... with --test_tag_filters=nogo | Run the selected Nogo owners; compiling another target does not establish that their checks passed. |
| [Unit tests (cgroupv1)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L379)<br>`make unit-tests` | unit-v1 · configured v1 unit owners | Run the AMD64 cgroup-v1 configuration. The AMD64 smoke leaf has no v1 witness. |
| [Unit tests (cgroupv2)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L387)<br>`make unit-tests` | unit · configured AMD64 unit owners | Only atomicbitops ran; the receipt records no cgroup-v2 witness. Compare current roots, environment and exclusions. |
| [Unit tests (ARM64)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L395)<br>`make unit-tests` | unit · configured ARM64 unit owners | Only atomicbitops ran. Compare current roots, environment and exclusions. |
| [Container tests (cgroupv1)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L402)<br>`make container-tests` | container / container-v1 · configured runsc/container owners | Supply the row's cgroup mode, Docker fixture and usable KVM to the leaves that use it. |
| [Container tests (cgroupv2)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L412)<br>`make container-tests` | container / container-v1 · configured runsc/container owners | Supply the row's cgroup mode, Docker fixture and usable KVM to the leaves that use it. |
| [System call tests (AMD64)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L427)<br>`make OPTIONS=--config=syscalls-amd64 syscall-tests` | syscalls · //test/syscalls/... and //test/rtnetlink/... | Only native and systrap/shared uname ran. Account for other owners, ptrace, KVM, directfs/overlay, rtnetlink, public flags and case partitions. |
| [System call tests (ARM64)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L436)<br>`make OPTIONS=--config=syscalls-arm64 syscall-tests` | syscalls · ARM64 ptrace/systrap owners | Neither AMD64 uname invocation satisfies this ARM64 row. Native ARM64 execution with namespace privileges needs a demonstrated worker or an explicitly accepted substitute. |

</details>

<details><summary>Images and GPUs · 9 rows</summary>

| Upstream row and command | Fork lane or target | Remaining check |
|---|---|---|
| [GPU Smoke Tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L222)<br>`make sudo TARGETS=//tools/gpu:main ARGS="install --latest"`<br>`make gpu-smoke-tests`<br>`make sudo TARGETS=//tools/gpu:main ARGS="validate_checksum"` | No matching fork lane identified | Requires a GPU worker, the specified Linux driver setup and the row's runtime workload; CPU image builds do not test the device. |
| [GPU Tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L238) · conditional GPU<br>`make sudo TARGETS=//tools/gpu:main ARGS="install --latest"`<br>`make gpu-all-tests` | No matching fork lane identified | Requires a GPU worker, the specified Linux driver setup and the row's runtime workload; CPU image builds do not test the device. |
| [COS GPU Tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L256) · conditional GPU<br>`make cos-gpu-all-tests` | No matching fork lane identified | Requires the COS canary GPU worker, its drivers and the GPU runtime suite. A COS driver-catalog test only checks metadata. |
| [CUDA tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L263) · conditional GPU<br>`make sudo TARGETS=//tools/gpu:main ARGS="install --latest"`<br>`make cuda-12-8-tests` | No matching fork lane identified | Requires a GPU worker, the specified Linux driver setup and the row's runtime workload; CPU image builds do not test the device. |
| [All GPU Drivers Test](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L276) · conditional GPU<br>`tools/gpu/all_drivers_test.sh` | No matching fork lane identified | Requires a GPU worker, the specified Linux driver setup and the row's runtime workload; CPU image builds do not test the device. |
| [Test CPU test images (x86_64)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L309) · conditional images<br>`make ARCH=x86_64 test-cpu-images` | cpu-images · //test/docker:cpu_image_sources_{arch}_test | Preserve per-architecture image availability/build checks and partition coverage. Runtime testing is a separate job. |
| [Test GPU/ML test images (x86_64)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L317) · conditional images<br>`make ARCH=x86_64 test-gpu-images` | gpu-images · //test/docker:gpu_image_sources_{arch}_test | Cover the source manifest/build path on this architecture. This job can run without accelerator hardware. |
| [Test CPU test images (aarch64)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L325) · conditional images<br>`make ARCH=aarch64 test-cpu-images` | cpu-images · //test/docker:cpu_image_sources_{arch}_test | Preserve per-architecture image availability/build checks and partition coverage. Runtime testing is a separate job. |
| [Test GPU/ML test images (aarch64)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L333) · conditional images<br>`make ARCH=aarch64 test-gpu-images` | gpu-images · //test/docker:gpu_image_sources_{arch}_test | Cover the source manifest/build path on this architecture. This job can run without accelerator hardware. |

</details>

<details><summary>Host and integration · 24 rows</summary>

| Upstream row and command | Fork lane or target | Remaining check |
|---|---|---|
| [Kubernetes smoke test](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L491)<br>`make kubernetes-smoke-test` | kubernetes · //test/kubernetes/tests:kind_test | Boot the declared runtime and cluster with Ubuntu/cgroup-v2 and satisfy the requested KVM placement. |
| [Docker tests (cgroupv1)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L502)<br>`make docker-tests` | docker / docker-v1 · //test/docker:owned_tests | Supply Docker, the installed runtime, the row's CPU and cgroup mode and the current case selection; keep its eight partitions accounted for. |
| [Docker tests (cgroupv2)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L514)<br>`make docker-tests` | docker / docker-v1 · //test/docker:owned_tests | Supply Docker, the installed runtime, the row's CPU and cgroup mode and the current case selection; keep its eight partitions accounted for. |
| [Docker tests (ARM64)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L526)<br>`make docker-tests` | docker / docker-v1 · //test/docker:owned_tests | Supply Docker, the installed runtime, the row's CPU and cgroup mode and the current case selection; keep its eight partitions accounted for. |
| [Overlay tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L537)<br>`make overlay-tests` | overlay · //test/docker:overlay_tests | Provide the modern Linux kernel and Docker overlay fixture and execute all selected cases. |
| [Port-Forward tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L548)<br>`make portforward-tests` | portforward · //test/root:portforward_test_owned | Exercise the installed runtime and actual host port-forward endpoints. |
| [Host network tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L557)<br>`make hostnet-tests` | hostnet · //test/docker:hostnet_tests | Provide Docker and the installed runtime with the host network configuration. |
| [gVisor GSO tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L568)<br>`make swgso-tests` | swgso · //test/docker:swgso_tests | Exercise software GSO using the row's installed runtime and network fixture. |
| [gVisor network plugin tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L579)<br>`make plugin-network-tests` | plugin-network · //test/docker:plugin_network_tests | Provide the plugin binary plus its network/device fixture; building the plugin does not exercise the link. |
| [Do tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L588)<br>`make do-tests` | do · //:do_tests | Run the actual runsc do cases. |
| [KVM tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L595)<br>`make kvm-tests` | kvm · //pkg/sentry/platform/kvm:kvm_test and //test/docker:kvm_tests | Requires usable KVM and installed runtime. Historical 17-owner KVM run used host /dev/kvm; select a capable RBE pool for the remote graph. |
| [Sandbox posture tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L606)<br>`make sandbox-posture-tests` | posture · //test/root:sandbox_posture_test_owned | Preserve the posture variants and check which need KVM or a privileged VM. |
| [Root tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L615)<br>`make root-tests` | root · //test/root:root_test_owned and //test/rbe:systemd_fixture_test | Provide the actual privileged Linux/systemd fixture and installed runtime. |
| [Systrap tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L624)<br>`make systrap-tests` | No matching fork lane identified | Preserve the systrap-specific invocation of integration/image tests and both runtime installs. The KVM agent label describes placement, not the platform that these cases exercise. |
| [Fsstress test](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L635)<br>`make fsstress-test` | fsstress · //test/fsstress:fsstress_test_owned | Run the fsstress image under the installed runtime on the required kernel. |
| [Containerd 1.7.31 tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L645)<br>`make containerd-test-1.7.31` | containerd · //test/root:crictl_test_owned | Keep the exact containerd version, cgroup-v2, CRI configuration and installed shim; the four versions are distinct. |
| [Containerd 2.0.8 tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L654)<br>`make containerd-test-2.0.8` | containerd · //test/root:crictl_test_owned | Keep the exact containerd version, cgroup-v2, CRI configuration and installed shim; the four versions are distinct. |
| [Containerd 2.1.7 tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L663)<br>`make containerd-test-2.1.7` | containerd · //test/root:crictl_test_owned | Keep the exact containerd version, cgroup-v2, CRI configuration and installed shim; the four versions are distinct. |
| [Containerd 2.2.3 tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L672)<br>`make containerd-test-2.2.3` | containerd · //test/root:crictl_test_owned | Keep the exact containerd version, cgroup-v2, CRI configuration and installed shim; the four versions are distinct. |
| [Podman](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L681)<br>`sudo RUNTIME_ARGS=--directfs=false ./test/podman/run.sh`<br>`sudo RUNTIME_ARGS=--directfs=true ./test/podman/run.sh` | podman · //test/podman:podman_test | Exercise both directfs=false and directfs=true in the Podman fixture. |
| [Moby tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L692)<br>`make moby-tests` | moby · //test/moby:moby_owned | Preserve the Moby suite, installed runtime and Docker cgroup-v2 environment. |
| [bwrap tests (AMD64)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L807)<br>`make bwrap-tests` | bwrap · //runsc/cmd/alias/bwrap:bwrap_integration_test | Run the installed alias with usable user/mount namespaces on the specified CPU. |
| [bwrap tests (ARM64)](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L815)<br>`make bwrap-tests` | bwrap · //runsc/cmd/alias/bwrap:bwrap_integration_test | Run the installed alias with usable user/mount namespaces on the specified CPU. |
| [Benchmarks smoke test](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L885)<br>`make benchmark-platforms BENCHMARKS_TARGETS=test/benchmarks/base:startup_test BENCHMARKS_FILTER=BenchmarkStartupEmpty BENCHMARKS_OPTIONS=-test.benchtime=1ns` | startup · //test/benchmarks/base:startup_test_owned | Preserve the exact benchmark filter, 1ns benchtime, runtime variants and KVM placement; official benchmark uploads have separate branch policy. |

</details>

<details><summary>Networking · 5 rows</summary>

| Upstream row and command | Fork lane or target | Remaining check |
|---|---|---|
| [Nftables docker tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L714)<br>`make nftables-tests` | nftables · //test/nftables:owned_tests | Run the Docker-mode nftables suite with its kernel modules. |
| [Nftables native syscall tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L722)<br>`make nftables-syscall-runc-tests` | No matching fork lane identified | Run socket_netlink_netfilter_test under runc as root in the nftables image with nfnetlink and nf_tables loaded; the Docker suite is a different contract. |
| [IPTables tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L730)<br>`make iptables-tests` | iptables · //test/iptables:owned_tests | Execute the current iptables suite and account for all eight partitions. |
| [Packetdrill tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L739)<br>`make packetdrill-tests` | packetdrill · //test/packetdrill:owned_tests | Run packetdrill against the row's modern-kernel Docker/network fixture. |
| [Packetimpact tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L747)<br>`make packetimpact-tests` | packetimpact · //test/packetimpact/tests:all_tests | Exercise the DUT and testbench with the required packet-network privileges. |

</details>

<details><summary>Language runtimes · 5 rows</summary>

| Upstream row and command | Fork lane or target | Remaining check |
|---|---|---|
| [PHP runtime tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L758)<br>`make php8.5.11-runtime-tests RUNTIME_ARGS=--directfs` | language-directfs · //test/runtimes:directfs_tests | Retain the exact release, directfs, upstream case filters, test lists and partitions; the similarly named goferfs rows run on master. |
| [Java runtime tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L767)<br>`make java21-runtime-tests RUNTIME_ARGS=--directfs` | language-directfs · //test/runtimes:directfs_tests | Retain the exact release, directfs, upstream case filters, test lists and partitions; the similarly named goferfs rows run on master. |
| [Go runtime tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L777)<br>`make go1.22-runtime-tests RUNTIME_ARGS=--directfs` | language-directfs · //test/runtimes:directfs_tests | Retain the exact release, directfs, upstream case filters, test lists and partitions; the similarly named goferfs rows run on master. |
| [NodeJS runtime tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L786)<br>`make nodejs22.2.0-runtime-tests RUNTIME_ARGS=--directfs` | language-directfs · //test/runtimes:directfs_tests | Retain the exact release, directfs, upstream case filters, test lists and partitions; the similarly named goferfs rows run on master. |
| [Python runtime tests](https://github.com/google/gvisor/blob/f51a205a8/.buildkite/pipeline.yaml#L796)<br>`make python3.12.3-runtime-tests RUNTIME_ARGS=--directfs` | language-directfs · //test/runtimes:directfs_tests | Retain the exact release, directfs, upstream case filters, test lists and partitions; the similarly named goferfs rows run on master. |

</details>
