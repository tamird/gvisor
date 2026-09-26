# BuildBuddy pilot

The optional `rbe.yaml` pipeline builds runsc for AMD64 and ARM64 and runs its
Nogo checks on the existing AMD64 Buildkite queue. Build actions use BuildBuddy
remote execution; the Bazel coordinator and Nogo test processes run directly on
the Linux CI worker. This pipeline does not start the custom builder image. The
ordinary pipeline and privileged test setup are unchanged.

Both targets use AMD64 remote workers, including for cross-compilation to ARM64.
The Nogo test executes an analysis tool built for the execution platform, so its
local coordinator must also be AMD64. This pilot does not execute ARM64 runsc or
syscall tests; those still need a native ARM64 test worker and compatible test
tools. It requires no self-hosted ARM64 BuildBuddy pool.

The pilot requires the hermetic LLVM C/C++ and BPF toolchains, SDK headers in Go
action inputs, an executable goimports launcher, and the Linux target platforms
that leave pure/cgo selection to the Go build settings.

Before uploading the pipeline, a CI operator must provision
`/tmp/gvisor-buildbuddy.bazelrc` on the AMD64 queue, owned by the Buildkite agent with
mode `0600`. It must contain the BuildBuddy authentication settings:

```bazelrc
common --remote_header=x-buildbuddy-api-key=YOUR_API_KEY
common --bes_header=x-buildbuddy-api-key=YOUR_API_KEY
```

Use the CI secret provisioning mechanism to create the file. Keep credentials
out of the repository and pipeline environment. Only trusted jobs should have
access to agents carrying this file; manually selecting this pipeline does not
isolate credentials from other jobs on the same agent.

From an authorized Buildkite job, upload the pilot explicitly:

```sh
buildkite-agent pipeline upload .buildkite/rbe.yaml
```

Each step downloads the native Bazel release selected by `.bazelversion` and
verifies it against `.buildkite/scripts/bazel.sha256`. Update that manifest from
the official release checksums when changing the Bazel version. The script uses
a temporary directory for the binary, shuts down Bazel when it exits, and removes
the downloaded binary. It retains the normal Bazel cache and outputs so the
post-command hook can upload failed test logs.

Bazel reads the workspace settings and the authentication file directly,
without the worker's home/system RC files or Make's GCS cache setting. The RBE
configurations select the requested target and AMD64 execution in a pinned
official Ubuntu 22.04 image, compress cache transfers, download build outputs,
and keep file paths in the build event stream for the existing failure-artifact
hook. Remote failures do not fall back to local compilation, and automatic
retries are disabled so a failed pilot remains visible.
