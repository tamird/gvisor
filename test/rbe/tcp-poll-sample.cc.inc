#ifdef __linux__
TEST_P(TcpSocketTest, HalfCloseReadinessLocal) {
  auto check = [&](short events, short expected) {
    SCOPED_TRACE(::testing::Message() << "poll events=" << events);
    struct pollfd pfd = {accepted_.get(), events, 0};
    EXPECT_THAT(RetryEINTR(poll)(&pfd, 1, 0),
                SyscallSucceedsWithValue(expected != 0 ? 1 : 0));
    EXPECT_EQ(pfd.revents, expected);
  };

  check(POLLRDHUP, 0);
  ASSERT_THAT(shutdown(accepted_.get(), SHUT_RD), SyscallSucceeds());
  check(POLLRDHUP, POLLRDHUP);
  check(POLLIN | POLLRDHUP, POLLIN | POLLRDHUP);
  check(POLLRDHUP, POLLRDHUP);

  // Keep the peer open so only the local shutdown can supply POLLHUP.
  ASSERT_THAT(shutdown(accepted_.get(), SHUT_WR), SyscallSucceeds());
  check(0, POLLHUP);
  check(POLLRDHUP, POLLRDHUP | POLLHUP);
}

TEST_P(TcpSocketTest, HalfCloseReadinessPeer) {
  int fd;
  ASSERT_THAT(fd = epoll_create1(EPOLL_CLOEXEC), SyscallSucceeds());
  FileDescriptor epoll_fd(fd);
  struct epoll_event event = {};
  event.events = EPOLLRDHUP;
  ASSERT_THAT(epoll_ctl(epoll_fd.get(), EPOLL_CTL_ADD, accepted_.get(), &event),
              SyscallSucceeds());
  ASSERT_THAT(RetryEINTR(epoll_wait)(epoll_fd.get(), &event, 1, 0),
              SyscallSucceedsWithValue(0));

  ASSERT_THAT(shutdown(connected_.get(), SHUT_WR), SyscallSucceeds());
  ASSERT_THAT(RetryEINTR(epoll_wait)(epoll_fd.get(), &event, 1, kTimeoutMillis),
              SyscallSucceedsWithValue(1));
  EXPECT_EQ(static_cast<uint32_t>(event.events), EPOLLRDHUP);
}
#endif  // __linux__

