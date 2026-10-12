// Copyright 2026 The gVisor Authors.
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

#include "tools/ioctl_sniffer/ioctl_hook.h"

#include <dlfcn.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <optional>
#include <string>

#include "gtest/gtest.h"
#include "test/util/cleanup.h"
#include "test/util/file_descriptor.h"
#include "test/util/memory_util.h"
#include "test/util/socket_util.h"
#include "test/util/test_util.h"
#include "tools/ioctl_sniffer/ioctl.pb.h"

namespace gvisor {
namespace testing {
namespace {

constexpr char kSocketEnv[] = "GVISOR_IOCTL_SNIFFER_SOCKET_PATH";

TEST(IoctlHookTest, PreservesErrnoOnInvalidDescriptor) {
  void* handle =
      dlopen(RunfilePath("tools/ioctl_sniffer/libioctl_hook.so").c_str(),
             RTLD_NOW | RTLD_LOCAL);
  ASSERT_NE(handle, nullptr) << dlerror();
  const Cleanup close_hook([handle] { dlclose(handle); });
  auto hooked_ioctl = reinterpret_cast<libc_ioctl>(dlsym(handle, "ioctl"));
  ASSERT_NE(hooked_ioctl, nullptr) << dlerror();

  // The real ioctl fails with EBADF. The hook's subsequent readlink on
  // /proc/self/fd/-1 fails with ENOENT, which must not replace that errno.
  errno = 0;
  const int result = hooked_ioctl(-1, 0, nullptr);
  const int result_errno = errno;
  EXPECT_EQ(result, -1);
  EXPECT_EQ(result_errno, EBADF);
}

TEST(IoctlHookTest, CapturesUnreadableArgumentsWithoutFaulting) {
  // No GPU is required. A regular file returns ENOTTY before touching argp,
  // while its path makes the real hook report the call. The isolated native
  // test worker provides a writable /dev; ordinary unprivileged runs skip.
  SKIP_IF(geteuid() != 0);
  std::string file_name = "/dev/nvidia-sniffer-XXXXXX";
  const FileDescriptor file(mkstemp(file_name.data()));
  ASSERT_THAT(file.get(), SyscallSucceeds());
  const Cleanup unlink_file([&file_name] { unlink(file_name.c_str()); });

  void* handle =
      dlopen(RunfilePath("tools/ioctl_sniffer/libioctl_hook.so").c_str(),
             RTLD_NOW | RTLD_LOCAL);
  ASSERT_NE(handle, nullptr) << dlerror();
  const Cleanup close_hook([handle] { dlclose(handle); });
  auto hooked_ioctl = reinterpret_cast<libc_ioctl>(dlsym(handle, "ioctl"));
  ASSERT_NE(hooked_ioctl, nullptr) << dlerror();

  const auto addr = ASSERT_NO_ERRNO_AND_VALUE(UniqueUnixAddr(false, AF_UNIX));
  const FileDescriptor listener(socket(AF_UNIX, SOCK_STREAM, 0));
  ASSERT_THAT(listener.get(), SyscallSucceeds());
  ASSERT_THAT(bind(listener.get(), reinterpret_cast<const sockaddr*>(&addr),
                   sizeof(addr)),
              SyscallSucceeds());
  const Cleanup unlink_socket([&addr] { unlink(addr.sun_path); });
  ASSERT_THAT(listen(listener.get(), 1), SyscallSucceeds());

  std::optional<std::string> previous;
  if (const char* value = std::getenv(kSocketEnv); value != nullptr) {
    previous = value;
  }
  const Cleanup restore_env([&previous] {
    if (previous.has_value()) {
      setenv(kSocketEnv, previous->c_str(), 1);
    } else {
      unsetenv(kSocketEnv);
    }
  });
  ASSERT_THAT(setenv(kSocketEnv, addr.sun_path, 1), SyscallSucceeds());

  auto mapping = ASSERT_NO_ERRNO_AND_VALUE(
      MmapAnon(2 * kPageSize, PROT_READ | PROT_WRITE, MAP_PRIVATE));
  char* memory = static_cast<char*>(mapping.ptr());
  memset(memory, 'a', kPageSize);
  ASSERT_THAT(mprotect(memory + kPageSize, kPageSize, PROT_NONE),
              SyscallSucceeds());
  const std::array<void*, 4> arguments = {
      memory, memory + kPageSize, memory + kPageSize - 16, memory + kPageSize};
  const std::array<size_t, 4> sizes = {32, 32, 32, 0};
  const std::array<size_t, 4> captured_sizes = {32, 0, 16, 0};
  const std::array<int, 4> capture_errors = {0, EFAULT, 0, 0};

  // Isolate the old reporter's SIGSEGV so the failure is a named assertion.
  // These four small frames fit in the socket buffer before the parent reads.
  // The child uses the parent's listener, rather than rebuilding the fixture.
  const std::string previous_style = GTEST_FLAG_GET(death_test_style);
  const Cleanup restore_style(
      [&previous_style] { GTEST_FLAG_SET(death_test_style, previous_style); });
  GTEST_FLAG_SET(death_test_style, "fast");
  ASSERT_EXIT(
      {
        for (size_t i = 0; i < arguments.size(); ++i) {
          const uint64_t request =
              _IOC(_IOC_READ | _IOC_WRITE, 'F', 0x20, sizes[i]);
          const int result = hooked_ioctl(file.get(), request, arguments[i]);
          if (result != -1 || errno != ENOTTY) {
            _exit(1);
          }
        }
        _exit(0);
      },
      ::testing::ExitedWithCode(0), "");

  const FileDescriptor connection(
      RetryEINTR(accept)(listener.get(), nullptr, nullptr));
  ASSERT_THAT(connection.get(), SyscallSucceeds());
  for (size_t i = 0; i < arguments.size(); ++i) {
    SCOPED_TRACE(i);
    std::array<unsigned char, sizeof(uint64_t)> header;
    ASSERT_THAT(ReadFd(connection.get(), header.data(), header.size()),
                SyscallSucceedsWithValue(header.size()));
    uint64_t size = 0;
    for (size_t j = 0; j < header.size(); ++j) {
      size |= static_cast<uint64_t>(header[j]) << (8 * j);
    }
    ASSERT_LE(size, 4096);
    std::string encoded(size, '\0');
    ASSERT_THAT(ReadFd(connection.get(), encoded.data(), encoded.size()),
                SyscallSucceedsWithValue(encoded.size()));
    Ioctl actual;
    ASSERT_TRUE(actual.ParseFromString(encoded));
    EXPECT_EQ(actual.fd_path(), file_name);
    EXPECT_EQ(actual.request(),
              _IOC(_IOC_READ | _IOC_WRITE, 'F', 0x20, sizes[i]));
    EXPECT_EQ(actual.ret(), -1);
    EXPECT_EQ(actual.arg_data(), std::string(captured_sizes[i], 'a'));
    EXPECT_EQ(actual.arg_data_errno(), capture_errors[i]);
  }
  char trailing;
  EXPECT_THAT(ReadFd(connection.get(), &trailing, 1),
              SyscallSucceedsWithValue(0));
}

}  // namespace
}  // namespace testing
}  // namespace gvisor
