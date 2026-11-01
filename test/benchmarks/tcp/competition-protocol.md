# Does excluding RACK-lost packets from flight change the bandwidth share?

Compare 5107630c4 and 0a4bcdacd. Both include the previously measured
RACK delivery and SACK observation changes. The second sender adds the
two remaining production hunks from google/gvisor#15432. The other
hunks, which copy segment flags and advance a rewound write cursor,
are already present in the first sender.

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
The isolated cases on google/gvisor#15432 remain relevant.
