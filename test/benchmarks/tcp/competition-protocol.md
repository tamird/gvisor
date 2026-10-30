# Competing-flow comparison

Use the existing 45-second, 20 Mbit/s, 100 ms, 100-packet ordered WAN
with two simultaneous single-stream senders, gVisor in the first lane
and Linux in the second. Both select the same congestion control.
Replay seed 16738; alternate implementation order across pairs A and B
for each of Reno and CUBIC. No cross-algorithm pooling. The earlier
stock versus #16738 comparison is retained as independent evidence.

Compare combined #16738 and #15634 at 777852bfe to the same sender
with the RACK observation amendment at 5107630c4. No zero-ReoWnd debug
code. Verify the exact product tree and launched helper SHA in each arm.

Repair timing: use only definitely missing original transmissions on
traces whose sender-receiver observation order reconciles in both
directions. For each first hole in a loss epoch, time the interval from
an arriving ACK covering at least three later original segments to the
actual retransmission of that hole. Count repairs already sent before the
threshold separately. Report denominator, median and count above 10 ms
per transfer and record both directions' losses. Call the measured delay
consistently shorter only if the median is lower in both matched pairs;
an unmatchable trace or no eligible repairs makes that claim unavailable.
Keep every TCP_INFO flag transition bracketed by socket sample times.

Bandwidth: report both senders' receiver goodput bounds over their common
measurement interval and gVisor's conservative share interval. Call the
observed share higher only if the amended lower bound exceeds the old
upper bound in both pairs. A lower bound of at least one half in both
pairs establishes at least an equal share on this fixture. Faster repair
alone says nothing about shares. These are descriptive trials, with no
significance or general fairness claim.

Prior mixed-path captures, with the same settings and reporting method:
- https://github.com/tamird/gvisor/actions/runs/41059496617/artifacts/12978564248
- https://github.com/tamird/gvisor/actions/runs/41081332294/artifacts/12991405464
Earlier standalone wire controls and the saved observation amendment:
- https://github.com/tamird/gvisor/actions/runs/41091511027/artifacts/12994247763
