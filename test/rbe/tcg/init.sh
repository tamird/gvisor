#!/bin/bash
# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# PID 1 inside the disposable full-system guest.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
/bin/busybox mount -t devtmpfs devtmpfs /dev
exec </dev/console >/dev/console 2>&1
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mkdir -p /dev/pts /dev/shm /sys/fs/cgroup
mount -t devpts devpts /dev/pts
mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /dev/shm
ln -s /proc/self/fd /dev/fd
mount -t cgroup2 none /sys/fs/cgroup
modprobe 9pnet_virtio
modprobe 9p
mount -t 9p -o trans=virtio,version=9p2000.L,ro input /input
mount -t 9p -o trans=virtio,version=9p2000.L result /result
finish() {
  local status=$?
  trap - EXIT
  set +e
  # When QEMU runs unprivileged, guest root must not replace the export owner.
  if [[ -d /work/outputs ]]; then cp -a --no-preserve=ownership /work/outputs /result/ || status=125; fi
  if [[ -d /work/harness ]]; then cp -a --no-preserve=ownership /work/harness /result/ || status=125; fi
  if [[ -f /work/test.xml ]]; then cp /work/test.xml /result/test.xml || status=125; fi
  if [[ -f /work/shard_status ]]; then cp /work/shard_status /result/shard_status || status=125; fi
  printf '%s\n' "$status" > /result/exit_status
  sync
  /bin/busybox poweroff -f
}
trap finish EXIT
# Generated from the selected image declaration, not the outer worker.
# shellcheck disable=SC1091
source /etc/gvisor-test-kernel
uname -a | tee /result/kernel.txt
# Kernel options determine which native syscall features the guest can test.
cp /etc/gvisor-test-kernel.config /result/kernel.config
[[ "$(uname -r)" == "${expected_kernel_release:?}" ]]
page_size="$(getconf PAGESIZE)"
printf 'Guest page size: %s\n' "$page_size" | tee /result/page-size.txt
[[ "$page_size" == "${expected_page_size:?}" ]]
if [[ "${expected_architecture:?}" == amd64 ]]; then
  # Host KVM access alone does not prove the CPU exposes virtualization to this
  # extra guest layer. Require the vendor module and device before the payload.
  flags=
  while read -r key colon flags; do
    [[ "$key" == flags && "$colon" == : ]] && break
  done < /proc/cpuinfo
  case " $flags " in
    *" vmx "*) module=kvm_intel ;;
    *" svm "*) module=kvm_amd ;;
    *) echo 'The guest CPU does not expose VMX or SVM.'; exit 1 ;;
  esac
  modprobe "$module"
  [[ -c /dev/kvm && -r /dev/kvm && -w /dev/kvm ]]
  printf 'Guest KVM module: %s\n' "$module" | tee /result/kvm.txt
fi
mount -t ext4 /dev/vda /work
mkdir -p /work/payload /work/tmp
chmod 1777 /work/tmp
# Gofer's read-only root remount needs its own mount, not the scratch disk root.
mount --bind /work/tmp /work/tmp
ip link set lo up
# Raw netfilter sockopts do not autoload their legacy handlers.
# https://github.com/torvalds/linux/blob/fd73f4a66/net/netfilter/nf_sockopt.c#L61-L85
modprobe ip_tables
modprobe ip6_tables
modprobe nf_conntrack
# Native descriptor tests duplicate descriptors above 1023; PID 1 starts with
# Linux's 1024 soft and 4096 hard limits, below the ordinary test workers.
cat /proc/self/limits > /result/limits-before.txt
ulimit -n 65536
cat /proc/self/limits > /result/limits.txt
/bin/busybox tar -xf /input/payload.tar -C /work/payload
/bin/bash /input/launch.sh
