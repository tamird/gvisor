# Does counting original and retransmitted packets separately improve TCP progress?

Compare 6241e4df3 and b0d2148f4. Both senders use the same accepted-byte
count for partial writes and face the same peer packet tests. The second
sender counts original transmissions and repairs separately when deciding
whether the congestion window has room for another packet.

Run both senders against Linux on the same native worker. Keep the
100 ms, 20 Mbit/s, 100-packet shared queue and both listener barriers.
Run Reno and CUBIC separately, with implementation order reversed in the
second pair for each. Check the actual helper hashes, receiver intervals,
first connect attempts, and both ends of the wire.

For each algorithm, count a higher receiver share only when the new
lower bound exceeds the matched original upper bound in both trials.
Report the time from three later SACKed segments to the first repair,
including its denominator and early repairs. A throughput change cannot
establish that the flight estimate is correct on every ACK sequence.
For each trial, report the longest interval in which the sender transmitted
no new sequence bytes, how many packets it repeated during that interval,
and the observed capture span. Count a shorter stall for an algorithm
only when the interval shrinks in both matched pairs.
