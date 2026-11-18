Native AMD64 VFIO consumer/build qualification

Exact candidate5d776df85f08eec3cf37a9c2a78f34e1f173b2b2, parent1f33fe3003455db178b97f3b43d3a012f1611994.
The change is a conflict-only replay ofc7aee950; original PR source remains untouched.

Build the full opaque release and all affected gofer/device-proxy/runner and new
VFIO test targets through canonical Make/Bazel. Run existing lisafs/groups/runner
owners with native CAP_SETGID via Bazel's run_under=sudo -E, explicitly preserving
verbose Go test XML. Collect actual complete unique XML shards immediately,
requiring named owner cases and no failures, errors or skips. Then source/C++ lint.
No custom source tests, local project execution, cloud permission change, secret,
remote cache/executor or new tool/cache home is introduced. Native host dependency
installation follows the established hosted Linux plans; AppArmor userns control
is applied before builds when present.

180m whole-script cap plus120s termination grace bounds the individually capped
steps. Cleanup caps97s including kill grace, reclaiming only task results and
shutting down Bazel. The source never switches or changes; final tracked/index
cleanliness remains a dispatcher gate.

This is deliberately only consumer/build qualification. VFIO Open is compiled,
not executed. No hardware, ARM, TPU application or performance claim is made.
A separate candidate-bound reuse of the existing KVM guest/emulated PCI harness
is in preparation; its earlier authenticated remote-executor configuration will
not be silently inherited. The original public reviewer scope question remains
open; no general O_PATH migration has been adopted.
