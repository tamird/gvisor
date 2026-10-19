// Copyright 2018 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

#include <string>

#include "gmock/gmock.h"
#include "gtest/gtest.h"
#include "absl/flags/flag.h"
#include "absl/time/time.h"
#include "test/util/cleanup.h"
#include "test/util/file_descriptor.h"
#include "test/util/logging.h"
#include "test/util/multiprocess_util.h"
#include "test/util/posix_error.h"
#include "test/util/test_util.h"
#include "test/util/thread_util.h"
#include "test/util/time_util.h"

ABSL_FLAG(bool, test_child, false,
          "If true, run the ExitAllThreads child workload.");
ABSL_FLAG(int, exit_control_fd, -1,
          "If set, wait for a byte on this socket before the child exits.");

namespace gvisor {
namespace testing {

namespace {

void TestExit(int code) {
  pid_t pid = fork();
  if (pid == 0) {
    _exit(code);
  }

  ASSERT_THAT(pid, SyscallSucceeds());

  int status;
  EXPECT_THAT(RetryEINTR(waitpid)(pid, &status, 0), SyscallSucceeds());
  EXPECT_TRUE(WIFEXITED(status) && WEXITSTATUS(status) == code) << status;
}

TEST(ExitTest, Success) { TestExit(0); }

TEST(ExitTest, Failure) { TestExit(1); }

// This test ensures that a process's file descriptors are closed when it calls
// exit(). In order to test this, the parent tries to read from a pipe whose
// write end is held by the child. While the read is blocking, the child exits,
// which should cause the parent to read 0 bytes due to EOF.
TEST(ExitTest, CloseFds) {
  int pipe_fds[2];
  ASSERT_THAT(pipe(pipe_fds), SyscallSucceeds());

  FileDescriptor read_fd(pipe_fds[0]);
  FileDescriptor write_fd(pipe_fds[1]);

  pid_t pid = fork();
  if (pid == 0) {
    read_fd.CloseSignalSafe();

    SleepSafe(absl::Seconds(10));

    _exit(0);
  }

  EXPECT_THAT(pid, SyscallSucceeds());

  write_fd.reset();

  char buf[10];
  EXPECT_THAT(ReadFd(read_fd.get(), buf, sizeof(buf)),
              SyscallSucceedsWithValue(0));
}

TEST(ExitTest, ExitAllThreads) {
  pid_t child_pid = -1;
  int execve_errno = 0;
  auto cleanup = ASSERT_NO_ERRNO_AND_VALUE(
      ForkAndExec("/proc/self/exe", {"/proc/self/exe", "--test_child"}, {},
                  nullptr, &child_pid, &execve_errno));
  ASSERT_GT(child_pid, 0);
  ASSERT_EQ(execve_errno, 0);

  int status;
  EXPECT_THAT(RetryEINTR(waitpid)(child_pid, &status, 0), SyscallSucceeds());
  EXPECT_TRUE(WIFEXITED(status) && WEXITSTATUS(status) == 0) << status;
}

void RunChild() {
  const int control = absl::GetFlag(FLAGS_exit_control_fd);
  if (control >= 0) {
    // Bound the controlled child even if its parent fails before releasing it.
    alarm(30);
  }
  ScopedThread t([control] {
    if (control >= 0) {
      char token = 'R';
      TEST_PCHECK(write(control, &token, 1) == 1);
      TEST_PCHECK(RetryEINTR(read)(control, &token, 1) == 1);
    }
    _exit(0);
  });
  t.Join();
  // Should not be reached
  abort();
}

// Check the exit/readiness boundary independently from the draining read.
// No bytes are written to the data pipe, so HUP must be followed by EOF rather
// than buffered data. A ready token only means that the reader is about to call
// read(); it does not establish that the kernel has already blocked that read.
void TestPipeEOFWhenChildReaped(bool retain_writer) {
  int data[2];
  ASSERT_THAT(pipe(data), SyscallSucceeds());
  FileDescriptor read_fd(data[0]);
  FileDescriptor write_fd(data[1]);

  int control[2];
  ASSERT_THAT(socketpair(AF_UNIX, SOCK_STREAM, 0, control), SyscallSucceeds());
  FileDescriptor parent_control(control[0]);
  FileDescriptor child_control(control[1]);
  pid_t writer = -1;
  int execve_errno = 0;
  auto writer_cleanup = ASSERT_NO_ERRNO_AND_VALUE(ForkAndExec(
      "/proc/self/exe",
      {"/proc/self/exe", "--test_child",
       "--exit_control_fd=" + std::to_string(child_control.get())},
      {},
      [&] {
        read_fd.CloseSignalSafe();
        parent_control.CloseSignalSafe();
      },
      &writer, &execve_errno));
  ASSERT_EQ(execve_errno, 0);
  child_control.reset();

  struct pollfd writer_ready = {parent_control.get(), POLLIN, 0};
  ASSERT_THAT(poll(&writer_ready, 1, 10000), SyscallSucceedsWithValue(1));
  char token;
  ASSERT_THAT(read(parent_control.get(), &token, 1),
              SyscallSucceedsWithValue(1));
  ASSERT_EQ(token, 'R');  // The writer's second thread is running.

  int report[2];
  ASSERT_THAT(socketpair(AF_UNIX, SOCK_STREAM, 0, report), SyscallSucceeds());
  FileDescriptor parent_report(report[0]);
  FileDescriptor child_report(report[1]);
  pid_t reader = fork();
  if (reader == 0) {
    // Do not let the drain helper itself keep the data pipe's writer alive.
    write_fd.CloseSignalSafe();
    parent_control.CloseSignalSafe();
    parent_report.CloseSignalSafe();
    alarm(30);
    char byte = 'R';
    TEST_PCHECK(write(child_report.get(), &byte, 1) == 1);
    const ssize_t n = RetryEINTR(read)(read_fd.get(), &byte, 1);
    const int result = n < 0 ? -errno : static_cast<int>(n);
    TEST_PCHECK(write(child_report.get(), &result, sizeof(result)) ==
                sizeof(result));
    _exit(0);
  }
  ASSERT_THAT(reader, SyscallSucceeds());
  auto reader_cleanup = Cleanup([reader] {
    kill(reader, SIGKILL);
    RetryEINTR(waitpid)(reader, nullptr, 0);
  });
  child_report.reset();
  if (!retain_writer) {
    write_fd.reset();
  }

  struct pollfd reader_ready = {parent_report.get(), POLLIN, 0};
  ASSERT_THAT(poll(&reader_ready, 1, 10000), SyscallSucceedsWithValue(1));
  ASSERT_THAT(read(parent_report.get(), &token, 1),
              SyscallSucceedsWithValue(1));
  ASSERT_EQ(token, 'R');

  ASSERT_THAT(write(parent_control.get(), &token, 1),
              SyscallSucceedsWithValue(1));
  int status;
  ASSERT_THAT(RetryEINTR(waitpid)(writer, &status, 0),
              SyscallSucceedsWithValue(writer));
  writer_cleanup.Release();
  ASSERT_TRUE(WIFEXITED(status)) << status;
  ASSERT_EQ(WEXITSTATUS(status), 0);

  struct pollfd pipe_state = {read_fd.get(), POLLIN | POLLHUP, 0};
  if (retain_writer) {
    // Reaping the child is insufficient while the coordinator owns a writer.
    ASSERT_THAT(poll(&pipe_state, 1, 0), SyscallSucceedsWithValue(0));
    ASSERT_THAT(poll(&reader_ready, 1, 0), SyscallSucceedsWithValue(0));
    write_fd.reset();
  }
  ASSERT_THAT(poll(&pipe_state, 1, 10000), SyscallSucceedsWithValue(1));
  ASSERT_EQ(pipe_state.revents, POLLHUP);
  ASSERT_THAT(poll(&reader_ready, 1, 10000), SyscallSucceedsWithValue(1));
  int result;
  ASSERT_THAT(ReadFd(parent_report.get(), &result, sizeof(result)),
              SyscallSucceedsWithValue(sizeof(result)));
  EXPECT_EQ(result, 0) << "drain returned bytes or negative errno";
  ASSERT_THAT(RetryEINTR(waitpid)(reader, &status, 0),
              SyscallSucceedsWithValue(reader));
  reader_cleanup.Release();
  ASSERT_TRUE(WIFEXITED(status)) << status;
  EXPECT_EQ(WEXITSTATUS(status), 0);
}

TEST(ExitTest, PipeEOFWhenChildReaped) { TestPipeEOFWhenChildReaped(false); }

TEST(ExitTest, PipeEOFWhenLastWriterClosed) {
  TestPipeEOFWhenChildReaped(true);
}

// SIGKILL of zombied thread group does not change exit status.
TEST(ExitTest, SigkillZombieGroup) {
  int pipe_fds[2];
  ASSERT_THAT(pipe(pipe_fds), SyscallSucceeds());

  FileDescriptor read_fd(pipe_fds[0]);
  FileDescriptor write_fd(pipe_fds[1]);

  pid_t pid = fork();
  if (pid == 0) {
    read_fd.CloseSignalSafe();

    _exit(0);
  }

  EXPECT_THAT(pid, SyscallSucceeds());
  write_fd.reset();

  // Wait for pipe to automatically close to indicate that the child is zombied.
  char buf[10];
  EXPECT_THAT(ReadFd(read_fd.get(), buf, sizeof(buf)),
              SyscallSucceedsWithValue(0));

  EXPECT_THAT(kill(pid, SIGKILL), SyscallSucceeds());

  // SIGKILL did not change exit status.
  int status;
  EXPECT_THAT(RetryEINTR(waitpid)(pid, &status, 0), SyscallSucceeds());
  EXPECT_TRUE(WIFEXITED(status) && WEXITSTATUS(status) == 0) << status;
}

// Variant of SigkillZombieGroup using exit(2) instead of exit_group(2).
TEST(ExitTest, SigkillZombieThread) {
  int pipe_fds[2];
  ASSERT_THAT(pipe(pipe_fds), SyscallSucceeds());

  FileDescriptor read_fd(pipe_fds[0]);
  FileDescriptor write_fd(pipe_fds[1]);

  pid_t pid = fork();
  if (pid == 0) {
    read_fd.CloseSignalSafe();

    syscall(SYS_exit, 0);
  }

  EXPECT_THAT(pid, SyscallSucceeds());
  write_fd.reset();

  // Wait for pipe to automatically close to indicate that the child is zombied.
  char buf[10];
  EXPECT_THAT(ReadFd(read_fd.get(), buf, sizeof(buf)),
              SyscallSucceedsWithValue(0));

  EXPECT_THAT(kill(pid, SIGKILL), SyscallSucceeds());

  // SIGKILL did not change exit status.
  int status;
  EXPECT_THAT(RetryEINTR(waitpid)(pid, &status, 0), SyscallSucceeds());
  EXPECT_TRUE(WIFEXITED(status) && WEXITSTATUS(status) == 0) << status;
}

}  // namespace

}  // namespace testing
}  // namespace gvisor

int main(int argc, char** argv) {
  gvisor::testing::TestInit(&argc, &argv);

  if (absl::GetFlag(FLAGS_test_child)) {
    gvisor::testing::RunChild();
    return 1;
  }

  return gvisor::testing::RunAllTests();
}
