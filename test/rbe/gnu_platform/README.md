This fork-only repository selects the existing AMD64 GNU compiler worker,
including its 2 GB memory estimate. Copy this directory before checking out
the product source, then select it with ordinary Bazel options:

```sh
bazel test \
  --inject_repository=tcp_gnu=/path/to/gnu_platform \
  --host_platform=@tcp_gnu//:gnu \
  --extra_execution_platforms=@tcp_gnu//:gnu \
  //pkg/waiter:waiter_nogo
```

Remote execution and authentication still come from the qualification harness.
This is an execution platform; select the product target architecture separately.
Jobs requiring more memory can define a platform with
`parents = ["@tcp_gnu//:gnu"]` and override `EstimatedMemory` in its
`exec_properties`. Existing per-action overrides remain applicable.

The 2 GB estimate matches the shared image qualification profile. The two
original kernel analyzer actions omitted `EstimatedMemory` and were killed;
their enforced limits were not recorded. They completed with the 2 GB estimate
in Actions run 41347908441, artifact 13099588299. This does not establish that
every analyzer fits in 2 GB.
