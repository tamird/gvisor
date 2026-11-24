// Copyright 2023 The gVisor Authors.
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

// Package filter_bench_test benchmarks the speed of the seccomp-bpf filters.
package filter_bench_test

import (
	"fmt"
	"testing"

	"golang.org/x/sys/unix"
	"gvisor.dev/gvisor/pkg/abi/linux"
	"gvisor.dev/gvisor/pkg/seccomp"
	"gvisor.dev/gvisor/pkg/sentry/devices/nvproxy/nvconf"
	"gvisor.dev/gvisor/pkg/sentry/platform/kvm"
	"gvisor.dev/gvisor/pkg/sentry/platform/systrap"
	"gvisor.dev/gvisor/runsc/boot/filter/config"
	"gvisor.dev/gvisor/test/secbench"
	"gvisor.dev/gvisor/test/secbench/secbenchdef"
)

// BenchmarkSentrySystrap benchmarks the seccomp filters used by the Sentry
// using the Systrap platform.
func BenchmarkSentrySystrap(b *testing.B) {
	opts := config.Options{
		Platform: (&systrap.Systrap{}).SeccompInfo(),
	}
	rules, denyRules := config.Rules(opts)
	secbench.Run(b, secbench.BenchFromSyscallRules(
		b,
		"Postgres",
		secbenchdef.Profile{
			Arch: linux.AUDIT_ARCH_X86_64,
			Sequences: []secbenchdef.Sequence{
				// Top 10 syscalls captured by running Postgres in a runsc container
				// and running `pgbench` against it. Weights are the number of times
				// each syscall was called.
				{Name: "futex", Weight: 870063, Syscalls: secbenchdef.Single(unix.SYS_FUTEX, 0, linux.FUTEX_WAKE)},
				{Name: "nanosleep", Weight: 275649, Syscalls: secbenchdef.NanosleepZero.Seq()},
				{Name: "sendmmsg", Weight: 160201, Syscalls: secbenchdef.Single(unix.SYS_SENDMMSG, secbenchdef.NonExistentFD, 0, 0, unix.MSG_DONTWAIT)},
				{Name: "fstat", Weight: 115769, Syscalls: secbenchdef.Single(unix.SYS_FSTAT, secbenchdef.NonExistentFD)},
				{Name: "ppoll", Weight: 69749, Syscalls: secbenchdef.PPollNonExistent.Seq()},
				{Name: "fsync", Weight: 23131, Syscalls: secbenchdef.Single(unix.SYS_FSYNC, secbenchdef.NonExistentFD)},
				{Name: "pwrite64", Weight: 14096, Syscalls: secbenchdef.Single(unix.SYS_PWRITE64, secbenchdef.NonExistentFD)},
				{Name: "epoll_pwait", Weight: 12266, Syscalls: secbenchdef.Single(unix.SYS_EPOLL_PWAIT, secbenchdef.NonExistentFD)},
				{Name: "close", Weight: 1991, Syscalls: secbenchdef.Single(unix.SYS_CLOSE, secbenchdef.NonExistentFD)},
				{Name: "getpid", Weight: 1413, Syscalls: secbenchdef.Single(unix.SYS_GETPID)},
			},
		},
		rules,
		denyRules,
		config.SeccompOptions(opts),
	))
}

// BenchmarkSentryKVM benchmarks the seccomp filters used by the Sentry
// using the KVM platform.
func BenchmarkSentryKVM(b *testing.B) {
	opts := config.Options{
		Platform: (&kvm.KVM{}).SeccompInfo(),
	}
	rules, denyRules := config.Rules(opts)
	secbench.Run(b, secbench.BenchFromSyscallRules(
		b,
		"Postgres",
		secbenchdef.Profile{
			Arch: linux.AUDIT_ARCH_X86_64,
			Sequences: []secbenchdef.Sequence{
				// Same procedure, but using the KVM platform instead.
				{Name: "futex", Weight: 3180352, Syscalls: secbenchdef.Single(unix.SYS_FUTEX, 0, linux.FUTEX_WAKE)},
				{Name: "ioctl", Weight: 2501786, Syscalls: secbenchdef.Single(unix.SYS_IOCTL, secbenchdef.NonExistentFD, kvm.KVM_RUN)},
				{Name: "rt_sigreturn", Weight: 2501695, Syscalls: secbenchdef.RTSigreturn.Seq()},
				{Name: "sendmmsg", Weight: 1490395, Syscalls: secbenchdef.Single(unix.SYS_SENDMMSG, secbenchdef.NonExistentFD, 0, 0, unix.MSG_DONTWAIT)},
				{Name: "nanosleep", Weight: 1217019, Syscalls: secbenchdef.NanosleepZero.Seq()},
				{Name: "fstat", Weight: 1068477, Syscalls: secbenchdef.Single(unix.SYS_FSTAT, secbenchdef.NonExistentFD)},
				{Name: "ppoll", Weight: 653137, Syscalls: secbenchdef.PPollNonExistent.Seq()},
				{Name: "fsync", Weight: 213320, Syscalls: secbenchdef.Single(unix.SYS_FSYNC, secbenchdef.NonExistentFD)},
				{Name: "pwrite64", Weight: 107603, Syscalls: secbenchdef.Single(unix.SYS_PWRITE64, secbenchdef.NonExistentFD)},
				{Name: "epoll_pwait", Weight: 29909, Syscalls: secbenchdef.Single(unix.SYS_EPOLL_PWAIT, secbenchdef.NonExistentFD)},
			},
		},
		rules,
		denyRules,
		config.SeccompOptions(opts),
	))
}

func BenchmarkNVProxyIoctl(b *testing.B) {
	opts := config.Options{
		Platform:    (&systrap.Systrap{}).SeccompInfo(),
		NVProxy:     true,
		NVProxyCaps: nvconf.ValidCapabilities,
	}
	rules, denyRules := config.Rules(opts)
	var sequences []secbenchdef.Sequence
	if err := rules.ForSingleArgument(unix.SYS_IOCTL, 1, func(v seccomp.ValueMatcher) error {
		if arg1Equal, isArg1Equal := v.(seccomp.EqualTo); isArg1Equal {
			sequences = append(sequences, secbenchdef.Sequence{
				Name:     fmt.Sprintf("ioctl_%d", arg1Equal),
				Weight:   1,
				Syscalls: secbenchdef.Single(unix.SYS_IOCTL, 0, uintptr(arg1Equal)),
			})
		}
		return nil
	}); err != nil {
		b.Fatalf("ioctl rules are not well-formed: %v", err)
	}
	secbench.Run(b, secbench.BenchFromSyscallRules(
		b,
		"nvproxy",
		secbenchdef.Profile{
			Arch:      linux.AUDIT_ARCH_X86_64,
			Sequences: sequences,
		},
		rules,
		denyRules,
		config.SeccompOptions(opts),
	))
}
