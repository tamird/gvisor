When a peer sends FIN and then sends data across the advertised window
edge, Linux acknowledges the data it has already received if its
receive queue is nonempty [1][2]. gVisor silently discarded the
segment even when the application had not read EOF.

Check the payload end before discarding data on a closing connection.
Keep a FIN received on an empty queue pending until the application
reads EOF. Peeking at EOF leaves it pending. When data is already
queued, gVisor treats the FIN as part of the last queued segment.

The packet tests exercise both kinds of reads. Linux and corrected
gVisor send the expected ACK while the FIN or the last byte remains
unread. Both stop responding to the probe once the application has
consumed it. Before the fix, gVisor missed the ACK in both cases [3].

[1]: https://github.com/torvalds/linux/commit/9ca48d616ed76b284f946667a3cb7961205c8ee3
[2]: https://github.com/torvalds/linux/commit/1e3bb184e94125bae7c1703472109a646d0f79d9
[3]: https://app.buildbuddy.io/invocation/2bcab414-3410-443c-b089-9121beeb043f
