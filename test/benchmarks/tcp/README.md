# TCP Benchmarks

This directory contains a standardized TCP benchmark. This helps to evaluate the
performance of netstack and native networking stacks under various conditions.

## `tcp_benchmark`

This benchmark allows TCP throughput testing under various conditions. The setup
consists of an iperf client, a client proxy, a server proxy and an iperf server.
The client proxy and server proxy abstract the network mechanism used to
communicate between the iperf client and server.

The setup looks like the following:

```
 +--------------+  (native)            +--------------+
 | iperf client |[lo @ 10.0.0.1]------>| client proxy |
 +--------------+                      +--------------+
                                    [client.0 @ 10.0.0.2]
                            (netstack)  |            |  (native)
                                        +------+-----+
                                               |
                                             [br0]
                                               |
          Network emulation applied ---> [wan.0:wan.1]
                                               |
                                             [br1]
                                               |
                                        +------+-----+
                            (netstack)  |            |  (native)
                                     [server.0 @ 10.0.0.3]
 +--------------+                      +--------------+
 | iperf server |<------[lo @ 10.0.0.4]| server proxy |
 +--------------+            (native)  +--------------+
```

Different configurations can be run using different arguments. For example:

*   Native test under normal internet conditions: `tcp_benchmark`
*   Native test under ideal conditions: `tcp_benchmark --ideal`
*   Netstack client under ideal conditions: `tcp_benchmark --client --ideal`
*   Netstack client with 5% packet loss: `tcp_benchmark --client --ideal --loss
    5`

Use `tcp_benchmark --help` for full arguments.

Select congestion control explicitly with `--congestion-control reno` or
`--congestion-control cubic`. Both native and netstack proxy sockets use the
selected algorithm; the default is Reno, including native sockets that previously
inherited the host default. `--cubic` remains an alias for selecting CUBIC.

For a controlled comparison, use the same rate, RTT, loss, queue, seed and offload
settings for each algorithm. For example, add `--ideal --latency 100 --rate 20
--loss 0.1 --queue-packets 100 --seed 1234` to each run. Latency is the total
round-trip delay, split across the two WAN devices; rate and queue limits apply
independently in each direction. A supplied seed requires support from both
`tc` and the host kernel. It reproduces the random generator input, not identical
packet histories from senders that transmit differently.

`--output-dir DIR` retains the exact invocation, requested settings, kernel/tool
versions, helper hashes, actual interface offload features, native namespace TCP
defaults, qdisc counters and the complete iperf output. Use a directory outside
`/tmp`, which the benchmark replaces inside its mount namespace. The reported
throughput is receiver goodput. An iperf failure or missing receiver summary
fails the run. The entire iperf operation is bounded to the requested
measurement duration plus 30 seconds for setup; connection-error retries have a
30-second window.

The benchmark requires Linux network and mount namespaces, veth, bridges and
`sch_netem`, plus `ip`, `tc`, `iperf3`, `ethtool`, `bc` and the existing helper
binaries. Build the helpers with Bazel; a successful build alone does not verify
these host capabilities. Preserve the actual environment and repeat trials
before making a performance comparison. The existing TCP probes are optional
diagnostics; recording every endpoint update can affect measured throughput. The
proxy logs resolved netstack options; native SACK/recovery/autotuning defaults
are recorded separately and are not assumed equivalent. The recorded native
default congestion-control sysctl does not override the selected per-socket
algorithm. The `cpu-time` metric is proxy user/system CPU seconds divided by the
requested measurement duration, including startup work, not total host-kernel
CPU cost.

For example, build and run the same scenario with each sender. Set `OUTPUT_DIR`
to a directory outside `/tmp` before running these commands on Linux:

```sh
for cc in reno cubic; do
  bazel run //test/benchmarks/tcp:tcp_benchmark -- \
    --linux-client --congestion-control "$cc" \
    --ideal --latency 100 --rate 20 --queue-packets 100 --duration 30 \
    --output-dir "$OUTPUT_DIR/linux-$cc"
  bazel run //test/benchmarks/tcp:tcp_benchmark -- \
    --client --congestion-control "$cc" \
    --ideal --latency 100 --rate 20 --queue-packets 100 --duration 30 \
    --output-dir "$OUTPUT_DIR/netstack-$cc"
done
```

The benchmark output identifies the sender stack and congestion-control
algorithm. These commands demonstrate the interface; use repeated, interleaved
trials with distinct output directories for a performance comparison. Inspect
receiver goodput together with queue drops, latency and recovery behavior rather
than treating one throughput number as a complete congestion-control result.
