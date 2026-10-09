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

With `--output-dir`, the receiver runs once and writes `receiver.json`, containing
its per-stream totals and one-second receiver intervals. `receiver-exit.txt` and
`receiver-stderr.txt` retain its status and diagnostics. The benchmark waits for
the receiver and uses `jq` to reject incomplete or failed reports. Zero received
bytes remain a measurement. The client's usual text output remains in
`iperf.txt`; its exit status is recorded separately from observation failures.
Receiver intervals describe delivered data, whereas client-side intervals can
include data buffered by the local proxy.

Add `--latency-probe` with `--output-dir` to retain native ICMP replies across the
same WAN queues, explicitly bound to the native client address. After a bounded
reachability check, the probe runs at ten packets per second with a 56-byte
payload. It starts with a five-second baseline before the proxies open their
forwarding connections, and continues until two seconds after the client
operation. `ping.txt` retains timestamps, reply
RTTs and outstanding-reply notices; `ping-stderr.txt` and `ping-exit.txt` preserve
diagnostics and status. Packet loss is an observation, while a probe that exits
early or reports an execution error fails capture.

`ping-phases.tsv` records probe launch/stop and client-operation boundaries with
Linux boot-time seconds and Unix timestamps. These are adjacent observations,
not an exact clock conversion. The client-operation interval includes connection
setup, retries and control exchanges. Treat these samples as native ICMP path
RTT under shared load, not Netstack TCP RTT or pure queue delay. Use the receiver
report to identify actual transfer intervals; its absolute connection timestamp
has only one-second precision and does not precisely align independent flows.
This probe requires AF_PACKET mode and rejects `--xdp`.

The benchmark requires Linux network and mount namespaces, veth, bridges and
`sch_netem`, plus `ip`, `tc`, `iperf3`, `ethtool`, `bc` and the existing helper
binaries. Retained receiver JSON also requires `jq`; `--latency-probe` requires
iputils `ping`. Their versions are captured when those features are enabled.
Build the helpers with Bazel; a successful build alone does not verify
these host capabilities. Preserve the actual environment and repeat trials
before making a performance comparison. The existing TCP probes are optional
diagnostics; recording every endpoint update can affect measured throughput. The
proxy logs resolved netstack options; native SACK/recovery/autotuning defaults
are recorded separately and are not assumed equivalent. The recorded native
default congestion-control sysctl does not override the selected per-socket
algorithm. The proxy's `packet_buffer_bytes` is the requested AF_PACKET socket
buffer size, not a TCP buffer measurement. The `cpu-time` metric is proxy
user/system CPU seconds divided by the
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
