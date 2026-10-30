Binding one end of a Unix socketpair to a pathname registered the
new host socket with epoll. If the peer had already sent a byte, the
bind could report a second edge for that same unread byte. Calling
listen on the connected endpoint could also make SO_ACCEPTCONN report
that it was a listener [1].

Register the host socket when the endpoint starts listening, and
remove the registration when it stops. A connected socketpair then
uses its peer for readiness even after it acquires a pathname.

The shared syscall test covers stream and sequenced-packet sockets.
Linux and corrected gVisor report one edge for the unread byte and
keep SO_ACCEPTCONN clear. The existing listener test still accepts a
connection [2][3]. The new test accepts either EINVAL or success
from listen because some Linux kernels returned success without
turning the socket into a listener [4].

This change builds on #16739.

[1]: https://app.buildbuddy.io/invocation/2a66c070-0940-4129-a984-3ce7ac7b0430
[2]: https://app.buildbuddy.io/invocation/3256a304-a0f0-4473-8cd4-1337ba2c486f
[3]: https://app.buildbuddy.io/invocation/718d047e-f898-4ee9-b221-866f3422bc3b
[4]: https://github.com/torvalds/linux/commit/f0d9c3ffc
