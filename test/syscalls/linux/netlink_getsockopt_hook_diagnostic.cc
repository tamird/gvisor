// Copyright 2026 The gVisor Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include <errno.h>
#include <fcntl.h>
#include <linux/bpf.h>
#include <linux/capability.h>
#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/utsname.h>
#include <unistd.h>

#include <iostream>

#include "gmock/gmock.h"
#include "gtest/gtest.h"
#include "test/util/cgroup_util.h"
#include "test/util/cleanup.h"
#include "test/util/file_descriptor.h"
#include "test/util/fs_util.h"
#include "test/util/linux_capability_util.h"
#include "test/util/logging.h"
#include "test/util/multiprocess_util.h"
#include "test/util/posix_error.h"
#include "test/util/temp_path.h"
#include "test/util/test_util.h"

namespace gvisor {
namespace testing {
namespace {

// Fork-only diagnostic: make the hook explicit instead of inferring it from
// EFAULT on a reviewer's worker. Never attach to the coordinator's cgroup.
TEST(NetlinkGetSockoptHookTest, CopyBeforeLengthRejection) {
  ASSERT_FALSE(IsRunningOnGvisor());
  struct utsname uts;
  ASSERT_THAT(uname(&uts), SyscallSucceeds());
  const bool admin = ASSERT_NO_ERRNO_AND_VALUE(HaveCapability(CAP_SYS_ADMIN));
  const bool bpf = ASSERT_NO_ERRNO_AND_VALUE(HaveCapability(CAP_BPF));
  std::cout << "kernel=" << uts.release << " arch=" << uts.machine
            << " CAP_SYS_ADMIN=" << admin << " CAP_BPF=" << bpf << std::endl;
  ASSERT_TRUE(admin)
      << "Controlled hook probe requires initial-namespace access";

  Mounter mounter(ASSERT_NO_ERRNO_AND_VALUE(TempPath::CreateDir()));
  Cgroup root = ASSERT_NO_ERRNO_AND_VALUE(mounter.MountCgroup2fs());
  Cgroup child = ASSERT_NO_ERRNO_AND_VALUE(root.CreateChild("netlink-hook"));
  Cleanup remove([&] { EXPECT_NO_ERRNO(child.Delete()); });
  FileDescriptor procs =
      ASSERT_NO_ERRNO_AND_VALUE(Open(child.Relpath("cgroup.procs"), O_WRONLY));
  FileDescriptor directory =
      ASSERT_NO_ERRNO_AND_VALUE(Open(child.Path(), O_RDONLY | O_DIRECTORY));
  const int procs_fd = procs.get();
  const auto check = [procs_fd](bool hooked) {
    ASSERT_THAT(
        InForkedProcess([procs_fd, hooked] {
          TEST_CHECK(write(procs_fd, "0", 1) == 1);
          const int fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE);
          TEST_PCHECK(fd >= 0);
          const int group = RTNLGRP_LINK;
          TEST_CHECK_SUCCESS(setsockopt(fd, SOL_NETLINK, NETLINK_ADD_MEMBERSHIP,
                                        &group, sizeof(group)));
          char full[256] = {};
          socklen_t full_len = sizeof(full);
          TEST_CHECK_SUCCESS(getsockopt(
              fd, SOL_NETLINK, NETLINK_LIST_MEMBERSHIPS, full, &full_len));
          TEST_CHECK(full_len >= sizeof(uint32_t));
          TEST_CHECK(full_len <= sizeof(full));
          TEST_CHECK(full_len % sizeof(uint32_t) == 0);
          socklen_t size = 0;
          const int query = getsockopt(
              fd, SOL_NETLINK, NETLINK_LIST_MEMBERSHIPS, nullptr, &size);
          TEST_CHECK(query == 0 || (hooked && query == -1 && errno == EFAULT));
          TEST_CHECK(size == full_len);
          for (socklen_t available = 0; available < full_len; ++available) {
            char buffer[sizeof(full)];
            memset(buffer, 'x', sizeof(buffer));
            socklen_t length = available;
            const int ret = getsockopt(
                fd, SOL_NETLINK, NETLINK_LIST_MEMBERSHIPS, buffer, &length);
            TEST_CHECK(ret == (hooked ? -1 : 0));
            if (hooked) {
              TEST_CHECK(errno == EFAULT);
            }
            TEST_CHECK(length == full_len);
            const size_t copied =
                available / sizeof(uint32_t) * sizeof(uint32_t);
            TEST_CHECK(memcmp(buffer, full, copied) == 0);
            for (size_t i = copied; i < sizeof(buffer); ++i) {
              TEST_CHECK(buffer[i] == 'x');
            }
          }
          TEST_CHECK_SUCCESS(close(fd));
          _exit(0);
        }),
        IsPosixErrorOkAndHolds(0));
    std::cout << "hooked=" << hooked << " all_short_lengths_checked"
              << std::endl;
  };
  check(false);
  ASSERT_FALSE(HasFatalFailure());

  const struct bpf_insn allow[] = {
      {BPF_ALU64 | BPF_MOV | BPF_K, BPF_REG_0, 0, 0, 1},
      {BPF_JMP | BPF_EXIT, 0, 0, 0, 0},
  };
  char verifier[4096] = {};
  union bpf_attr load = {};
  load.prog_type = BPF_PROG_TYPE_CGROUP_SOCKOPT;
  load.expected_attach_type = BPF_CGROUP_GETSOCKOPT;
  load.insn_cnt = sizeof(allow) / sizeof(allow[0]);
  load.insns = reinterpret_cast<uint64_t>(allow);
  load.license = reinterpret_cast<uint64_t>("Apache-2.0");
  load.log_level = 1;
  load.log_buf = reinterpret_cast<uint64_t>(verifier);
  load.log_size = sizeof(verifier);
  const int program_fd = syscall(__NR_bpf, BPF_PROG_LOAD, &load, sizeof(load));
  ASSERT_THAT(program_fd, SyscallSucceeds()) << verifier;
  FileDescriptor program(program_fd);
  union bpf_attr attach = {};
  attach.target_fd = directory.get();
  attach.attach_bpf_fd = program.get();
  attach.attach_type = BPF_CGROUP_GETSOCKOPT;
  ASSERT_THAT(syscall(__NR_bpf, BPF_PROG_ATTACH, &attach, sizeof(attach)),
              SyscallSucceeds());
  Cleanup detach([&] {
    EXPECT_THAT(syscall(__NR_bpf, BPF_PROG_DETACH, &attach, sizeof(attach)),
                SyscallSucceeds());
  });
  check(true);
}

}  // namespace
}  // namespace testing
}  // namespace gvisor
