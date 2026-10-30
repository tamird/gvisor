# Concurrent launch after both local listeners are ready

Use the identical amended TCP sender (5107630c4) in the two Reno and two
CUBIC trials, with Linux competing on the same bridge. Preserve source,
instrumentation, WAN lane, 45-second duration, 20 Mbit/s rate, 100 ms RTT,
100-packet queue and trial order from the eight-transfer comparison.
The 107.4–107.6 ms earlier Linux first full-size payload in the four
original amended trials was measured at the sender-facing taps. In all
four the extra time arose before the control cookie and data-socket SYN.
Both forwarding proxies dial before exposing their Linux listeners. The
original runner could begin iperf before the proxy was listening and
retries a failed connect every 100 ms. The old capture does not contain
the local connect attempts.

Start iperf only after both local Linux listeners are present. Check the
listener using ss without consuming a connection, and record all actual
iperf attempts. Do not change WAN transport, congestion control, receiver,
pacing, buffers, retries or copy code. Exact input sender sources and
actual helper hashes are recorded; the prior amended helper was
c3883f31b4009523de81041bd7dfebcc0a5e49513720a9e884dc3612016d9e84.

Determine alignment independently for each trial. Both data flows must
start within 15 milliseconds of one another (first payload of at least
1,000 bytes on each dominant data connection), at both sender egress and
the common receiver. Publish actual differences including signs, and
report an unaligned or absent trial without counting it as matched.
Record original control SYN, handshake and first control bytes separately
from the data SYN and bulk start. Also require that both iperf invocations
succeed on their first attempt after the recorded barrier.

Use actual receiver bytes over the conservative common interval for
each share bound, separately from packet loss and repair timing. Compare
each aligned trial with its own earlier arm. A higher share for an
algorithm requires non-overlap in that direction in both matched pairs.
A lower bound of at least one half in both pairs is required to observe
equal share. Even if all four align, the experiment provides two trials
per algorithm. Do not infer a general property of the TCP algorithms.

Original source, packet captures and report:
https://github.com/tamird/gvisor/actions/runs/41093629937/artifacts/12996880967
