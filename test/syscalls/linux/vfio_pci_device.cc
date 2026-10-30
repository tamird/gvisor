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

#include <fcntl.h>
#include <linux/vfio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/eventfd.h>
#include <sys/ioctl.h>
#include <unistd.h>

#include <array>
#include <cstddef>

#include "gmock/gmock.h"
#include "gtest/gtest.h"
#include "test/util/file_descriptor.h"
#include "test/util/test_util.h"

namespace gvisor {
namespace testing {
namespace {

class VfioPCIDeviceTest : public ::testing::Test {
 protected:
  void SetUp() override {
    const char* group_path = getenv("TEST_VFIO_GROUP");
    const char* device_name = getenv("TEST_VFIO_DEVICE");
    ASSERT_NE(group_path, nullptr);
    ASSERT_NE(device_name, nullptr);
    container_ = ASSERT_NO_ERRNO_AND_VALUE(Open("/dev/vfio/vfio", O_RDWR));
    group_ = ASSERT_NO_ERRNO_AND_VALUE(Open(group_path, O_RDWR));
    int container_fd = container_.get();
    ASSERT_THAT(ioctl(group_.get(), VFIO_GROUP_SET_CONTAINER, &container_fd),
                SyscallSucceeds());
    ASSERT_THAT(ioctl(container_.get(), VFIO_SET_IOMMU, VFIO_TYPE1v2_IOMMU),
                SyscallSucceeds());
    device_ = FileDescriptor(
        ioctl(group_.get(), VFIO_GROUP_GET_DEVICE_FD, device_name));
    ASSERT_THAT(device_.get(), SyscallSucceeds());
  }

  FileDescriptor container_;
  FileDescriptor group_;
  FileDescriptor device_;
};

TEST_F(VfioPCIDeviceTest, Open) {
  struct vfio_device_info info = {};
  info.argsz = sizeof(info);
  ASSERT_THAT(ioctl(device_.get(), VFIO_DEVICE_GET_INFO, &info),
              SyscallSucceeds());
  ASSERT_NE(info.flags & VFIO_DEVICE_FLAGS_PCI, 0);
}

TEST_F(VfioPCIDeviceTest, InterruptEventfdKeepsFullCounter) {
  struct vfio_irq_info irq = {};
  irq.argsz = sizeof(irq);
  for (unsigned int i : {VFIO_PCI_MSIX_IRQ_INDEX, VFIO_PCI_MSI_IRQ_INDEX,
                         VFIO_PCI_INTX_IRQ_INDEX}) {
    irq.index = i;
    ASSERT_THAT(ioctl(device_.get(), VFIO_DEVICE_GET_IRQ_INFO, &irq),
                SyscallSucceeds());
    if (irq.count && (irq.flags & VFIO_IRQ_INFO_EVENTFD)) {
      break;
    }
  }
  ASSERT_GT(irq.count, 0);
  ASSERT_NE(irq.flags & VFIO_IRQ_INFO_EVENTFD, 0);

  const FileDescriptor event(eventfd(0, EFD_NONBLOCK));
  ASSERT_THAT(event.get(), SyscallSucceeds());
  constexpr uint64_t saved = (uint64_t{1} << 32) + 1;
  ASSERT_THAT(write(event.get(), &saved, sizeof(saved)),
              SyscallSucceedsWithValue(sizeof(saved)));
  alignas(vfio_irq_set) std::array<std::byte,
                                  sizeof(vfio_irq_set) + sizeof(int)> buf{};
  auto* setup = reinterpret_cast<vfio_irq_set*>(buf.data());
  setup->argsz = buf.size();
  setup->flags = VFIO_IRQ_SET_DATA_EVENTFD | VFIO_IRQ_SET_ACTION_TRIGGER;
  setup->index = irq.index;
  setup->count = 1;
  const int fd = event.get();
  memcpy(setup->data, &fd, sizeof(fd));
  ASSERT_THAT(ioctl(device_.get(), VFIO_DEVICE_SET_IRQS, setup),
              SyscallSucceeds());
  uint64_t count;
  ASSERT_THAT(read(fd, &count, sizeof(count)),
              SyscallSucceedsWithValue(sizeof(count)));
  // The device may have delivered an interrupt after binding the eventfd.
  EXPECT_GE(count, saved);
  setup->argsz = sizeof(vfio_irq_set);
  setup->flags = VFIO_IRQ_SET_DATA_NONE | VFIO_IRQ_SET_ACTION_TRIGGER;
  setup->count = 0;
  EXPECT_THAT(ioctl(device_.get(), VFIO_DEVICE_SET_IRQS, setup),
              SyscallSucceeds());
}

}  // namespace
}  // namespace testing
}  // namespace gvisor
