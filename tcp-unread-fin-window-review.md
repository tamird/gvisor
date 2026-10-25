tcp: ACK window overruns while FIN is unread

After a peer sent FIN, netstack silently discarded a two-byte segment that started at the advertised right edge. Linux sends its current cumulative ACK when payload crosses that edge and the receive queue is nonempty [1]. The FIN flag itself does not count as payload for this check [2].

Check the payload end before applying closing-state handling. A FIN without queued data remains unread until the application consumes EOF; peeking at EOF leaves it unread. If data is queued, netstack represents FIN at the tail of that queue. The guard only runs after a peer FIN and does not change how established connections accept payload.

The packetimpact test keeps each socket in CLOSE_WAIT and probes both at the right edge and one byte inside it. With FIN alone, the socket must ACK before and after a peek, then send no ACK for one second after a consuming EOF read. With two bytes and FIN in one segment, it must ACK while one byte remains unread, then send no ACK after the application reads the final byte, before a separate EOF read. Linux can keep a separately arriving FIN in its own buffer [3]; the data-bearing test makes no assumption about coalescing across packets.

Land after #16377 [4]. That change reads EOF before probing closing states in three existing fixtures, preserving their empty-queue assumptions. The new test exercises the complementary unread case. The branches merge cleanly; the combined tree was not run.

The remote comparison of the final patch [5] passed both subtests on native Linux. Unpatched netstack timed out at the first unread-queue ACK in each subtest (native exit 3). With the fix, Linux and netstack passed both subtests (native exit 0). The aggregate remains FAIL because it retains the negative control.

An earlier remote comparison [6] tested the same production behavior on the initial fork revision fcd913e6b4. The fixed arm passed the standalone-FIN wire test, the no-window-shrinking and two receive-buffer-autotuning cases on AMD64 and ARM64, TCP Nogo, and goimports; the control timed out waiting for the unread-FIN ACK. The final revision changes the test and a source comment. Both comparisons ran source-verified patches on the hosted qualification base, not a direct build of the public fork tree [7].

[1]: https://github.com/torvalds/linux/commit/9ca48d616ed76b284f946667a3cb7961205c8ee3
[2]: https://github.com/torvalds/linux/commit/1e3bb184e94125bae7c1703472109a646d0f79d9
[3]: https://github.com/gregkh/linux/blob/v6.18.57/net/ipv4/tcp_input.c#L5320-L5340
[4]: https://github.com/google/gvisor/pull/16377
[5]: https://app.buildbuddy.io/invocation/2bcab414-3410-443c-b089-9121beeb043f
[6]: https://app.buildbuddy.io/invocation/96284b02-ca6c-47aa-99b3-2b0d59091519
[7]: https://github.com/tamird/gvisor/commit/7ae55f4cc07eb541c8d63442a343a3c2564dfd6f
