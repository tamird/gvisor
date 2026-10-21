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

#ifndef GVISOR_IMAGES_BENCHMARKS_IPERF_CONFIG_H_
#define GVISOR_IMAGES_BENCHMARKS_IPERF_CONFIG_H_

// iperf 2.1.5 configure.ac defaults for Linux with POSIX threads, IPv6,
// multicast and TCP statistics. Jammy also enables fast sampling.
// Both supported targets use little-endian Linux and the declared C toolchain.
#define VERSION "2.1.5"
#define Socklen_t socklen_t
#define _REENTRANT 1
#define NDEBUG 1

#define HAVE_AF_PACKET 1
#define HAVE_CLOCK_GETTIME 1
#define HAVE_CLOCK_NANOSLEEP 1
#define HAVE_DECL_CPU_SET 1
#define HAVE_DECL_IPV6_ADD_MEMBERSHIP 1
#define HAVE_DECL_IPV6_JOIN_GROUP 1
#define HAVE_DECL_IPV6_TCLASS 1
#define HAVE_DECL_IP_ADD_SOURCE_MEMBERSHIP 1
#define HAVE_DECL_IP_MULTICAST_ALL 1
#define HAVE_DECL_IP_TOS 1
#define HAVE_DECL_MCAST_JOIN_GROUP 1
#define HAVE_DECL_MCAST_JOIN_SOURCE_GROUP 1
#define HAVE_DECL_MSG_DONTWAIT 1
#define HAVE_DECL_MSG_PEEK 1
#define HAVE_DECL_MSG_WAITALL 1
#define HAVE_DECL_SIGALRM 1
#define HAVE_DECL_SO_BINDTODEVICE 1
#define HAVE_DECL_SO_DONTROUTE 1
#define HAVE_DECL_SO_MAX_PACING_RATE 1
#define HAVE_DECL_SO_REUSEPORT 1
#define HAVE_DECL_SO_TIMESTAMP 1
#define HAVE_DECL_TCP_INFO 1
#define HAVE_DECL_TCP_NODELAY 1
#define HAVE_DECL_TCP_NOTSENT_LOWAT 1
#define HAVE_DECL_TCP_WINDOW_CLAMP 1
#define HAVE_FASTSAMPLING 1
#define HAVE_FREOPEN 1
#define HAVE_GETTID_SYSCALL 1
#define HAVE_GETTIMEOFDAY 1
#define HAVE_IFADDRS_H 1
#define HAVE_INET_NTOP 1
#define HAVE_INET_PTON 1
#define HAVE_INT64_T 1
#define HAVE_IPV6 1
#define HAVE_IPV6_MULTICAST 1
#define HAVE_KALMAN 1
#define HAVE_LIBINTL_H 1
#define HAVE_LINUX_FILTER_H 1
#define HAVE_MLOCKALL 1
#define HAVE_MULTICAST 1
#define HAVE_NANOSLEEP 1
#define HAVE_NETDB_H 1
#define HAVE_NETINET_IN_H 1
#define HAVE_NETINET_TCP_H 1
#define HAVE_NET_IF_H 1
#define HAVE_POSIX_THREAD 1
#define HAVE_ROLE_REVERSAL_ID 1
#define HAVE_SCHED_SETSCHEDULER 1
#define HAVE_SCHED_YIELD 1
#define HAVE_SETITIMER 1
#define HAVE_SIGNAL_H 1
#define HAVE_SNPRINTF 1
#define HAVE_SSM_MULTICAST 1
#define HAVE_STDBOOL_H 1
#define HAVE_STRINGS_H 1
#define HAVE_STRUCT_GROUP_SOURCE_REQ 1
#define HAVE_STRUCT_IP_MREQ_SOURCE 1
#define HAVE_STRUCT_IP_MREQ_SOURCE_IMR_MULTIADDR_S_ADDR 1
#define HAVE_STRUCT_TCP_INFO_TCPI_TOTAL_RETRANS 1
#define HAVE_SYSLOG_H 1
#define HAVE_SYS_SOCKET_H 1
#define HAVE_TCP_STATS 1
#define HAVE_TUNTAP_TAP 1
#define HAVE_TUNTAP_TUN 1
#define HAVE_USLEEP 1

#endif  // GVISOR_IMAGES_BENCHMARKS_IPERF_CONFIG_H_
