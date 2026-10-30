Run only CUBIC pair B on the synchronized shared link. The original
schedule seed 16738 puts the baseline first for pair B, then the
intervention. Each transfer uses one gVisor CUBIC sender competing with
native Linux CUBIC in the same lanes for 45 seconds. The fixed 100 ms RTT,
20 Mbit/s rate, 100-packet queue, 1500-byte MTU, socket settings and
two-listener barrier are unchanged. Random netem loss, jitter, duplication
and seed were unset in the original and stay unset. Queue drops depend
on actual packet arrivals. The earlier loss sequence cannot be prescribed.

Build the same instrumented proxy twice. Change only the two packet
accounting hunks from #15432 on top of 5107630c4. Check the baseline
proxy against its retained digest and confirm different candidate binary.
Require successful first connection attempts and first full payload within
15 ms between senders at both WAN taps. Keep any unaligned pair or
missing result in the receipt without treating it as matched.

Measure receiver bytes over the common interval independently from the
sender's packet and state observations. Record each lane's repair count,
fresh sequence bytes, longest interval without fresh sequence, confirmed
forward and reverse losses, cwnd, outstanding, queued data, peer window
and ACK progress. The earlier B observation was zero new data across
42.667 s with a backlogged sender, repairs arriving, active recovery,
45–46 outstanding vs cwnd 5. Neither an improved receiver share nor
a brief idle interval is a reproduction of that whole stall. If the
control does not reproduce it, this pair cannot measure how #15432
affects that original trajectory. Do not infer general fairness from B.

Retained synchronous transfer: https://github.com/tamird/gvisor/actions/runs/41098212501/artifacts/12997387247
Manual-clock reduced witness: https://github.com/tamird/gvisor/actions/runs/41101513521/artifacts/12997669419
