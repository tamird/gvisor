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

#include <arpa/inet.h>
#include <fcntl.h>
#include <linux/capability.h>
#include <linux/if.h>
#include <linux/if_ether.h>
#include <linux/if_tun.h>
#include <netinet/ip.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <unistd.h>

#include <array>
#include <cerrno>
#include <cstring>

#include "gmock/gmock.h"
#include "gtest/gtest.h"
#include "test/syscalls/linux/socket_netlink_route_util.h"
#include "test/util/capability_util.h"
#include "test/util/file_descriptor.h"
#include "test/util/fs_util.h"
#include "test/util/linux_capability_util.h"
#include "test/util/posix_error.h"
#include "test/util/save_util.h"
#include "test/util/socket_util.h"
#include "test/util/test_util.h"

namespace gvisor {
namespace testing {
namespace {

class TuntapPacketLifetimeTest : public ::testing::Test {
 protected:
  void SetUp() override {
    SKIP_IF(!ASSERT_NO_ERRNO_AND_VALUE(HaveCapability(CAP_NET_ADMIN)));
    SKIP_IF(!ASSERT_NO_ERRNO_AND_VALUE(HaveCapability(CAP_NET_RAW)));

    tun_ = ASSERT_NO_ERRNO_AND_VALUE(Open("/dev/net/tun", O_RDWR));
    ifreq req = {};
    strcpy(req.ifr_name, "tun-packet");
    req.ifr_flags = IFF_TUN | IFF_NO_PI;
    ASSERT_THAT(ioctl(tun_.get(), TUNSETIFF, &req), SyscallSucceeds());
    const auto link = ASSERT_NO_ERRNO_AND_VALUE(GetLinkByName(req.ifr_name));
    ASSERT_NO_ERRNO(LinkChangeFlags(link.index, IFF_UP, IFF_UP));
    packet_ = ASSERT_NO_ERRNO_AND_VALUE(
        Socket(AF_PACKET, SOCK_RAW | SOCK_NONBLOCK, htons(ETH_P_IP)));

    // An ordinary IPv4 packet addressed outside this interface has no local
    // transport consumer. AF_PACKET still receives its complete contents.
    iphdr ip = {};
    ip.version = 4;
    ip.ihl = sizeof(ip) / 4;
    ip.tot_len = htons(payload_.size());
    ip.ttl = 64;
    ip.protocol = 253;  // RFC 3692 experimental protocol.
    ip.saddr = htonl(0xc0000201);
    ip.daddr = htonl(0xc0000202);
    ip.check = IPChecksum(ip);
    memcpy(payload_.data(), &ip, sizeof(ip));
    for (size_t i = sizeof(ip); i < payload_.size(); ++i) {
      payload_[i] = i;
    }
  }

  void QueuePacketAndCloseTun() {
    ASSERT_THAT(write(tun_.get(), payload_.data(), payload_.size()),
                SyscallSucceedsWithValue(payload_.size()));
    pollfd pfd = {.fd = packet_.get(), .events = POLLIN};
    ASSERT_THAT(RetryEINTR(poll)(&pfd, 1, 10000), SyscallSucceedsWithValue(1));
    ASSERT_NE(pfd.revents & POLLIN, 0);

    // Open TUN devices cannot be checkpointed. Only the packet socket and its
    // queued payload remain when cooperative checkpoints are re-enabled.
    tun_.reset();
    disable_save_.reset();
    MaybeSave();
  }

  DisableSave disable_save_;
  FileDescriptor tun_;
  FileDescriptor packet_;
  std::array<unsigned char, 128> payload_ = {};
};

TEST_F(TuntapPacketLifetimeTest, ReadAfterTunClose) {
  ASSERT_NO_FATAL_FAILURE(QueuePacketAndCloseTun());
  std::array<unsigned char, 129> received;
  ASSERT_THAT(recv(packet_.get(), received.data(), received.size(), 0),
              SyscallSucceedsWithValue(payload_.size()));
  EXPECT_EQ(memcmp(received.data(), payload_.data(), payload_.size()), 0);
}

TEST_F(TuntapPacketLifetimeTest, CloseUnreadSocket) {
  ASSERT_NO_FATAL_FAILURE(QueuePacketAndCloseTun());
  packet_.reset();
  MaybeSave();
}

TEST_F(TuntapPacketLifetimeTest, ExitWithUnreadSocket) {
  ASSERT_NO_FATAL_FAILURE(QueuePacketAndCloseTun());
  // Exercise task FD-table teardown instead of the C++ descriptor destructor.
  // The test runner creates a fresh sandbox for each named test.
  packet_.release();
}

TEST_F(TuntapPacketLifetimeTest, FaultingTailDoesNotInject) {
  iovec iov[] = {
      {.iov_base = payload_.data(), .iov_len = payload_.size()},
      {.iov_base = nullptr, .iov_len = 1},
  };
  ASSERT_THAT(writev(tun_.get(), iov, 2), SyscallFailsWithErrno(EFAULT));
  pollfd pfd = {.fd = packet_.get(), .events = POLLIN};
  ASSERT_THAT(poll(&pfd, 1, 0), SyscallSucceedsWithValue(0));
  tun_.reset();
  disable_save_.reset();
  MaybeSave();
}

}  // namespace
}  // namespace testing
}  // namespace gvisor
