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

Run `bazel run //test/benchmarks/tcp:tcp_benchmark -- --help` for all options.
The Linux host supplies iproute2 (including its netem distributions) and iperf3.
Turning off Linux offloads also needs ethtool. Bazel provides the two proxy
helpers. When invoking an installed binary directly, place `tcp_proxy` and
`nsjoin` beside it or specify their directory with `--helpers`.

The benchmark prints a Go benchmark result on stdout, for example:

```text
BenchmarkTCP/role=client/host-gso=true/host-gro=true 1 10.8 Mb/s 0.06 cpu-time
```

The rate comes from iperf3's aggregate sender result. CPU time is the selected
proxy's user and system CPU seconds divided by the requested duration.
`--client` and `--linux-client` select the client proxy. Otherwise the benchmark
reports the server proxy. Diagnostics and the raw iperf3 results go to stderr.

For example, collect client throughput and CPU use under different link delays:

```bash
set -o pipefail
for latency in 0 5 10 25 50 100; do
  tcp_benchmark --client --ideal --latency "$latency" |
    awk -v latency="$latency" '{print latency "," $3 "," $5}' || exit
done
```

Use `--linux-client` in place of `--client` to measure the native client.
To vary packet loss, hold `--latency` constant and change `--loss`.
