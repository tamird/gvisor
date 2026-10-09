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

## Shared-WAN flows

Add `--second-client linux` or `--second-client netstack` to run two independent
client sessions through the same WAN device pair. The first sender still uses
`--client` or the native default. `--second-congestion-control reno|cubic`
selects the second sender; omitting it uses the first sender's algorithm. Each
session uses the requested `--num-client-threads`, so use one stream per session
for an initial two-flow comparison.

The second client has its own namespace and veth attached to the existing
client-side bridge. Its native and proxy addresses are `10.0.0.5` and
`10.0.0.6`. The two sessions have separate native server proxy and iperf ports
in the existing server namespace. Both traverse the same `wan.0`/`wan.1`
queues; there is no second independent bottleneck.

`--second-start-delay SECONDS` delays the second flow's startup from the first
flow's launch. It defaults to zero and must be less than `--duration`. The
second measurement duration is the first duration minus that delay. Both proxy
sides and the second receiver start only when that flow is launched, avoiding
an idle preconnected control channel. Actual setup and connection times can
differ: the requested delay is not a promise that first data arrives at an
exact offset.

Shared-WAN mode requires `--output-dir`, IPv4, AF_PACKET and a native receiver;
`--server`, `--xdp` and per-proxy profile files are not supported in this mode.
Each `primary/` and `secondary/` directory retains its own proxy arguments,
client text, receiver JSON, process/cleanup statuses, benchmark row and
`flow-phases.tsv`. Flow phases record proxy launch and client-operation
boundaries with adjacent boot-time and Unix timestamps. The root directory
retains common settings, interface features, WAN qdisc snapshots and optional
ICMP observations. `topology-cleanup-exit.txt` records the shared namespace's
cleanup separately from each flow's cleanup.

For example, on the established Linux benchmark worker:

```sh
bazel run //test/benchmarks/tcp:tcp_benchmark -- \
  --client --congestion-control cubic \
  --second-client linux --second-congestion-control reno \
  --second-start-delay 30 --duration 120 \
  --ideal --latency 100 --rate 100 --queue-packets 1000 \
  --sack --disable-linux-gso --disable-linux-gro --latency-probe \
  --output-dir "$OUTPUT_DIR/cubic-reno-late-join"
```

Use receiver intervals and the recorded launch/operation bounds to identify
common overlap before comparing shares. Whole-session totals do not measure
late-join fairness. Independent receiver timestamps still have one-second
precision and precede measurement start; do not compute a precisely aligned
Jain index or convergence time without establishing the alignment bound. The
shared probe labels the entire interval as `flows-begin`/`flows-end`, including
both flows' setup margins. It remains native ICMP path RTT, not either TCP
stack's RTT or pure queue delay.

### Optional TCP observations

`--tcp-observations` requires `--output-dir` and a native server. Each
Netstack sender writes `tcp-netstack.json` in its flow directory. The proxy
samples registered TCP endpoints every 100 ms through `StateSnapshot`; its
per-packet probe remains disabled. Each record includes a string socket tuple,
congestion window, flight and SACK counts, peer window, send-buffer use,
RTT/RTO, recovery state and CUBIC estimates. Durations are nanoseconds.
`BootBeginNS` and `BootEndNS` bracket the snapshot on the VM's boot clock;
`UnixNS` is the later recording time. Sampling can be delayed by scheduling or
endpoint locking, so use actual bounds rather than assuming exact 10 Hz.

Native senders retain `tcp-native.txt`: bounded `ss -tinmH` queries of the
actual WAN destination and port, with begin/end uptime and Unix timestamps,
status and tool version. An empty query before connection setup is valid;
missing TCP_INFO fields are unknown. These records describe the Linux WAN
sender, not the Netstack proxy's local ingress leg. Compare only correctly
identified overlapping sockets and retain the sample timing uncertainty.

The existing `--client_tcp_probe_file` and `--server_tcp_probe_file` options
now write the same JSON record schema for explicit per-packet observations.
Their timestamps describe callback invocation, not the duration of the earlier
state copy. Packet and periodic capture are mutually exclusive. The old gob
encoder ignored errors for state containing private address/time fields; a
nonempty old file did not prove that a complete snapshot was captured.

The Netstack recorder retains at most 12,000 records in memory and writes them
after stopping its sampler. Reaching the cap, an encoding/write/close error, or
a failed native query makes capture incomplete and the proxy/flow unsuccessful;
raw partial output is retained. Closed or not-yet-connected endpoints increment
`Unavailable`, rather than supplying zero-valued TCP metrics. Cleanup stops and
reaps the native sampler and joins the Netstack sampler before output is closed.

These optional diagnostics add work. They are for distinguishing window,
application and recovery hypotheses, not uninstrumented throughput results.
`Outstanding` alone is not the remembered cwnd-limited signal, and send-buffer
usage includes data retained for retransmission. The recorded configured
congestion-control name is not an independent socket-option readback.
