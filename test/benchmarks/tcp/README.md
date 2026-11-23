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

Results use the Go benchmark format, with throughput reported in `Mb/s`.
The `cpu-time` metric is the selected client or server proxy's CPU time divided by
the test duration.
