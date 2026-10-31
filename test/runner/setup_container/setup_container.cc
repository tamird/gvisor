// Copyright 2021 The gVisor Authors.
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
#include <linux/capability.h>
#include <linux/sockios.h>
#include <net/if.h>
#include <sched.h>
#include <sys/ioctl.h>
#include <sys/mount.h>
#include <sys/socket.h>
#include <sys/statvfs.h>
#include <unistd.h>

#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <initializer_list>
#include <iostream>
#include <ostream>
#include <string>
#include <utility>
#include <vector>

#include "test/syscalls/linux/socket_netlink_util.h"
#include "test/util/capability_util.h"
#include "test/util/file_descriptor.h"
#include "test/util/linux_capability_util.h"
#include "test/util/posix_error.h"
#include "test/util/socket_util.h"

namespace gvisor {
namespace testing {

namespace {

struct NetworkAttribute {
  uint16_t type;
  const void* data;
  size_t size;
};

void AppendNetworkAttributes(std::vector<char>& message,
                             std::initializer_list<NetworkAttribute> attrs) {
  for (const auto& attr : attrs) {
    const size_t offset = RTA_ALIGN(message.size());
    message.resize(offset + RTA_SPACE(attr.size));
    const rtattr header = {
        .rta_len = static_cast<uint16_t>(RTA_LENGTH(attr.size)),
        .rta_type = attr.type,
    };
    memcpy(message.data() + offset, &header, sizeof(header));
    memcpy(message.data() + offset + RTA_LENGTH(0), attr.data, attr.size);
  }
}

PosixError ConfigureNetwork(const FileDescriptor& fd, uint16_t type,
                            const void* payload, size_t payload_size,
                            std::initializer_list<NetworkAttribute> attrs) {
  std::vector<char> message(NLMSG_SPACE(payload_size));
  memcpy(message.data() + NLMSG_HDRLEN, payload, payload_size);
  AppendNetworkAttributes(message, attrs);
  nlmsghdr header = {};
  constexpr uint32_t kSeq = 1;
  InitNetlinkHdr(&header, message.size(), type, kSeq,
                 NLM_F_REQUEST | NLM_F_ACK | NLM_F_CREATE | NLM_F_EXCL);
  memcpy(message.data(), &header, sizeof(header));
  return NetlinkRequestAckOrError(fd, kSeq, message.data(), message.size());
}

}  // namespace

// SetupExternalNetwork supplies a non-loopback interface for local UDP
// broadcast and multicast tests. A dummy link cannot reach another namespace;
// the caller's private network namespace owns the link and its lifetime.
PosixError SetupExternalNetwork() {
  FileDescriptor fd;
  ASSIGN_OR_RETURN_ERRNO(fd, NetlinkBoundSocket(NETLINK_ROUTE));
  constexpr char kName[] = "eth0";
  constexpr char kKind[] = "dummy";
  std::vector<char> link_info;
  AppendNetworkAttributes(link_info, {{IFLA_INFO_KIND, kKind, sizeof(kKind)}});
  ifinfomsg link = {};
  link.ifi_flags = IFF_UP | IFF_MULTICAST;
  link.ifi_change = IFF_UP | IFF_MULTICAST;
  RETURN_IF_ERRNO(
      ConfigureNetwork(fd, RTM_NEWLINK, &link, sizeof(link),
                       {{IFLA_IFNAME, kName, sizeof(kName)},
                        {IFLA_LINKINFO, link_info.data(), link_info.size()}}));

  const unsigned int index = if_nametoindex(kName);
  if (index == 0) {
    return PosixError(errno, "find fixture interface index");
  }
  const in_addr ipv4 = {.s_addr = htonl(0xc0000201)};  // 192.0.2.1
  const in_addr broadcast = {.s_addr = htonl(0xc00002ff)};
  ifaddrmsg addr = {};
  addr.ifa_family = AF_INET;
  addr.ifa_prefixlen = 24;
  addr.ifa_index = index;
  RETURN_IF_ERRNO(
      ConfigureNetwork(fd, RTM_NEWADDR, &addr, sizeof(addr),
                       {{IFA_LOCAL, &ipv4, sizeof(ipv4)},
                        {IFA_ADDRESS, &ipv4, sizeof(ipv4)},
                        {IFA_BROADCAST, &broadcast, sizeof(broadcast)}}));

  in6_addr ipv6 = {};
  if (inet_pton(AF_INET6, "2001:db8::1", &ipv6) != 1) {
    return PosixError(EINVAL, "invalid fixture IPv6 address");
  }
  addr.ifa_family = AF_INET6;
  addr.ifa_prefixlen = 64;
  addr.ifa_flags = IFA_F_NODAD;
  RETURN_IF_ERRNO(ConfigureNetwork(fd, RTM_NEWADDR, &addr, sizeof(addr),
                                   {{IFA_ADDRESS, &ipv6, sizeof(ipv6)}}));

  // No default route or peer is needed. These routes select the private
  // interface for local broadcast and IPv4 multicast delivery.
  for (const auto& [destination, prefix] :
       {std::pair<uint32_t, uint8_t>{0xe0000000, 4},
        std::pair<uint32_t, uint8_t>{0xffffffff, 32}}) {
    const in_addr address = {.s_addr = htonl(destination)};
    rtmsg route = {};
    route.rtm_family = AF_INET;
    route.rtm_dst_len = prefix;
    route.rtm_table = RT_TABLE_MAIN;
    route.rtm_protocol = RTPROT_BOOT;
    route.rtm_scope = RT_SCOPE_LINK;
    route.rtm_type = RTN_UNICAST;
    RETURN_IF_ERRNO(ConfigureNetwork(fd, RTM_NEWROUTE, &route, sizeof(route),
                                     {{RTA_DST, &address, sizeof(address)},
                                      {RTA_OIF, &index, sizeof(index)}}));
  }
  return NoError();
}

// EnableAtime preserves the test's host filesystem while providing the atime
// semantics needed by timestamp tests. This must run before creating a user
// namespace, which locks inherited noatime flags.
PosixError EnableAtime() {
  const char* tmpdir = getenv("TEST_TMPDIR");
  if (tmpdir == nullptr || *tmpdir == '\0') {
    return PosixError(EINVAL, "--require-atime requires TEST_TMPDIR");
  }
  struct statvfs fs = {};
  if (statvfs(tmpdir, &fs) == -1) {
    return PosixError(errno, "statvfs TEST_TMPDIR");
  }
  if ((fs.f_flag & (ST_NOATIME | ST_NODIRATIME)) == 0) {
    return NoError();
  }

  // Own the mount namespace and prevent propagation to the test executor.
  // Creating only a mount namespace retains the caller's mount capabilities.
  if (unshare(CLONE_NEWNS) == -1) {
    return PosixError(errno,
                      "atime setup requires CAP_SYS_ADMIN for CLONE_NEWNS");
  }
  if (mount(nullptr, "/", nullptr, MS_REC | MS_PRIVATE, nullptr) == -1) {
    return PosixError(errno, "make atime setup mounts private");
  }
  if (mount(tmpdir, tmpdir, nullptr, MS_BIND, nullptr) == -1) {
    return PosixError(errno, "bind TEST_TMPDIR for atime updates");
  }

  // Bind-remount changes only this mount, not the underlying superblock. Keep
  // its security flags, and preserve strictatime if only nodiratime was set.
  uint64_t flags = MS_BIND | MS_REMOUNT;
  if (fs.f_flag & ST_RDONLY) flags |= MS_RDONLY;
  if (fs.f_flag & ST_NOSUID) flags |= MS_NOSUID;
  if (fs.f_flag & ST_NODEV) flags |= MS_NODEV;
  if (fs.f_flag & ST_NOEXEC) flags |= MS_NOEXEC;
  // Linux reports ST_NOSYMFOLLOW even when libc predates the named constant.
  // The statvfs conversion preserves the kernel's f_flags bits.
  // https://github.com/torvalds/linux/blob/830b3c68c/include/linux/statfs.h#L44
  // https://github.com/torvalds/linux/blob/830b3c68c/include/uapi/linux/mount.h#L21
  // https://github.com/bminor/glibc/blob/3c03baca3/sysdeps/unix/sysv/linux/internal_statvfs.c#L86
  constexpr uint64_t kSTNosymfollow = 0x2000;
  constexpr uint64_t kMSNosymfollow = 0x100;
  if (fs.f_flag & kSTNosymfollow) flags |= kMSNosymfollow;
  flags |=
      (fs.f_flag & (ST_NOATIME | ST_RELATIME)) ? MS_RELATIME : MS_STRICTATIME;
  if (mount(nullptr, tmpdir, nullptr, flags, nullptr) == -1) {
    return PosixError(
        errno, "enable TEST_TMPDIR atime (inherited flags may be locked)");
  }
  return NoError();
}

// SetupContainer sets up the networking settings in the current container.
PosixError SetupContainer() {
  const PosixErrorOr<bool> have_net_admin = HaveCapability(CAP_NET_ADMIN);
  if (!have_net_admin.ok()) {
    std::cerr << "Cannot determine if we have CAP_NET_ADMIN." << std::endl;
    return have_net_admin.error();
  }
  if (have_net_admin.ValueOrDie()) {
    PosixErrorOr<FileDescriptor> sockfd = Socket(AF_INET, SOCK_DGRAM, 0);
    if (!sockfd.ok()) {
      std::cerr << "Cannot open socket." << std::endl;
      return sockfd.error();
    }
    int sock = sockfd.ValueOrDie().get();
    struct ifreq ifr = {};
    strncpy(ifr.ifr_name, "lo", IFNAMSIZ);
    if (ioctl(sock, SIOCGIFFLAGS, &ifr) == -1) {
      std::cerr << "Cannot get 'lo' flags: " << strerror(errno) << std::endl;
      return PosixError(errno);
    }
    if ((ifr.ifr_flags & IFF_UP) == 0) {
      ifr.ifr_flags |= IFF_UP;
      if (ioctl(sock, SIOCSIFFLAGS, &ifr) == -1) {
        std::cerr << "Cannot set 'lo' as UP: " << strerror(errno) << std::endl;
        return PosixError(errno);
      }
    }
  } else {
    std::cerr
        << "Capability CAP_NET_ADMIN not granted, so cannot bring up "
        << "'lo' interface. This may cause host-network-related tests to fail."
        << std::endl;
  }
  return NoError();
}

}  // namespace testing
}  // namespace gvisor

using ::gvisor::testing::EnableAtime;
using ::gvisor::testing::SetupContainer;
using ::gvisor::testing::SetupExternalNetwork;

// Binary setup_container initializes a test container, or prepares host atime
// semantics before the runner creates containers, then execs the given binary.
// Usage:
//   ./setup_container test_binary [arguments forwarded to test_binary...]
//   ./setup_container --external-network test_binary [arguments...]
//   ./setup_container --require-atime runner [arguments forwarded to runner...]
int main(int argc, char* argv[], char* envp[]) {
  if (argc < 2) {
    std::cerr << "Must provide arguments to exec." << std::endl;
    return 2;
  }
  int command = 1;
  if (strcmp(argv[1], "--require-atime") == 0) {
    if (argc < 3) {
      std::cerr << "Must provide arguments to exec." << std::endl;
      return 2;
    }
    auto err = EnableAtime();
    if (!err.ok()) {
      std::cerr << "Cannot prepare test atime: " << err << std::endl;
      return 1;
    }
    command = 2;
  } else if (!SetupContainer().ok()) {
    return 1;
  } else if (strcmp(argv[1], "--external-network") == 0) {
    if (argc < 3) {
      std::cerr << "Must provide arguments to exec." << std::endl;
      return 2;
    }
    const auto err = SetupExternalNetwork();
    if (!err.ok()) {
      std::cerr << "Cannot prepare test network: " << err << std::endl;
      return 1;
    }
    command = 2;
  }
  if (execve(argv[command], &argv[command], envp) == -1) {
    std::cerr << "execv returned errno " << errno << std::endl;
    return 1;
  }
}
