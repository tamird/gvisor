  int first = -1;
  socklen_t len = sizeof(first);
  ASSERT_THAT(getsockopt(s.get(), SOL_SOCKET, SO_ERROR, &first, &len),
              SyscallSucceeds());
  ASSERT_EQ(first, ECONNREFUSED);
  ASSERT_THAT(RetryEINTR(poll)(&poll_fd, 1, 0), SyscallSucceedsWithValue(1));
  EXPECT_EQ(poll_fd.revents, POLLHUP);

  int second = -1;
  ASSERT_THAT(getsockopt(s.get(), SOL_SOCKET, SO_ERROR, &second, &len),
              SyscallSucceeds());
  EXPECT_EQ(second, 0);

