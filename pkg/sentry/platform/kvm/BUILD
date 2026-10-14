load("//test/rbe/tcg:defs.bzl", "amd64_kvm_test", "arm64_tcg_test")
load("//tools:defs.bzl", "go_library", "go_test")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")
load("//tools/go_generics:defs.bzl", "go_template_instance")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_template_instance(
    name = "atomicptr_machine",
    out = "atomicptr_machine_unsafe.go",
    package = "kvm",
    prefix = "machine",
    template = "//pkg/sync/atomicptr:generic_atomicptr",
    types = {
        "Value": "machine",
    },
)

config_setting(
    name = "debug_build",
    values = {
        "compilation_mode": "dbg",
    },
)

go_library(
    name = "kvm",
    srcs = [
        "address_space.go",
        "address_space_amd64.go",
        "address_space_arm64.go",
        "atomicptr_machine_unsafe.go",
        "bluepill.go",
        "bluepill_allocator.go",
        "bluepill_amd64.go",
        "bluepill_amd64.s",
        "bluepill_amd64_unsafe.go",
        "bluepill_arm64.go",
        "bluepill_arm64.s",
        "bluepill_arm64_unsafe.go",
        "bluepill_fault.go",
        "bluepill_unsafe.go",
        "config.go",
        "context.go",
        "filters.go",
        "filters_amd64.go",
        "filters_arm64.go",
        "ioeventfd.go",
        "ioeventfd_unsafe.go",
        "kvm.go",
        "kvm_amd64.go",
        "kvm_amd64_unsafe.go",
        "kvm_arm64.go",
        "kvm_arm64_unsafe.go",
        "kvm_const.go",
        "kvm_const_amd64.go",
        "kvm_const_arm64.go",
        "kvm_profiling.go",
        "kvm_profiling_fake.go",
        "machine.go",
        "machine_amd64.go",
        "machine_amd64_unsafe.go",
        "machine_arm64.go",
        "machine_arm64_unsafe.go",
        "machine_cgo.go",
        "machine_unsafe.go",
        "physical_map.go",
        "physical_map_amd64.go",
        "physical_map_arm64.go",
        "seccomp_mmap_unsafe.go",
        "virtual_map.go",
    ],
    visibility = ["//pkg/sentry:internal"],
    deps = [
        "//pkg/abi/linux",
        "//pkg/atomicbitops",
        "//pkg/bitmap",
        "//pkg/context",
        "//pkg/cpuid",
        "//pkg/eventfd",
        "//pkg/fd",
        "//pkg/hostarch",
        "//pkg/hostos",
        "//pkg/hostsyscall",
        "//pkg/hosttid",
        "//pkg/log",
        "//pkg/metric",
        "//pkg/pinring",
        "//pkg/ring0",
        "//pkg/ring0/pagetables",
        "//pkg/seccomp",
        "//pkg/sentry/arch",
        "//pkg/sentry/arch/fpu",
        "//pkg/sentry/hostmm",
        "//pkg/sentry/memmap",
        "//pkg/sentry/platform",
        "//pkg/sentry/platform/interrupt",
        "//pkg/sentry/time",
        "//pkg/sigframe",
        "//pkg/sighandling",
        "//pkg/sync",
        "//pkg/timing",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_test(
    name = "kvm_test",
    srcs = [
        "kvm_amd64_test.go",
        "kvm_amd64_test.s",
        "kvm_arm64_test.go",
        "kvm_safecopy_test.go",
        "kvm_test.go",
        "virtual_map_test.go",
    ],
    embed = [":kvm"],
    exec_properties = docker_test_exec_properties(free_disk = "20GB"),
    # FIXME(gvisor.dev/issue/3374): Not working with all build systems.
    nogo = False,
    pure = True,
    tags = [
        "manual",
        "nogotsan",
        "requires-kvm",
    ],
    deps = [
        "//pkg/abi/linux",
        "//pkg/cpuid",
        "//pkg/eventfd",
        "//pkg/hostarch",
        "//pkg/hostsyscall",
        "//pkg/memutil",
        "//pkg/ring0",
        "//pkg/ring0/pagetables",
        "//pkg/safecopy",
        "//pkg/sentry/arch",
        "//pkg/sentry/arch/fpu",
        "//pkg/sentry/platform",
        "//pkg/sentry/platform/kvm/testutil",
        "//pkg/sentry/time",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_test(
    name = "kvm_cgo_test",
    srcs = [
        "kvm_amd64_test.go",
        "kvm_amd64_test.s",
        "kvm_arm64_test.go",
        "kvm_safecopy_test.go",
        "kvm_test.go",
        "virtual_map_test.go",
    ],
    embed = [":kvm"],
    # FIXME(gvisor.dev/issue/3374): Not working with all build systems.
    nogo = False,
    tags = [
        "nogotsan",
        "requires-kvm",
    ],
    deps = [
        "//pkg/abi/linux",
        "//pkg/cpuid",
        "//pkg/eventfd",
        "//pkg/hostarch",
        "//pkg/hostsyscall",
        "//pkg/memutil",
        "//pkg/ring0",
        "//pkg/ring0/pagetables",
        "//pkg/safecopy",
        "//pkg/sentry/arch",
        "//pkg/sentry/arch/fpu",
        "//pkg/sentry/platform",
        "//pkg/sentry/platform/kvm/testutil",
        "//pkg/sentry/time",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

# Qualify the same guest/fixture's nested KVM capability with the complete
# existing KVM package owner before selecting all container shards.
amd64_kvm_test(
    name = "cgroup_v1_guest_probe",
    size = "medium",
    guest_env = {"CGROUPV2": "false"},
    image = "//test/rbe/tcg:amd64_cgroup_v1_guest",
    memory_mib = 6144,
    payload = ":kvm_test",
    run_under = "//test/rbe:cgroup_v1",
    scratch_gib = 8,
    tags = [],
)

# Isolate KVM entry and exit with the existing ARM64 unit test body.
arm64_tcg_test(
    name = "kernel_syscall_arm64_tcg_test",
    size = "large",
    args = ["-test.run=^TestKernelSyscall$$"],
    arm64_el2 = True,
    guest_env = {
        "GO_TEST_RUN_FROM_BAZEL": "1",
        "GO_TEST_WRAP_TESTV": "1",
    },
    image = "//test/rbe/tcg:rc_guest",
    payload = ":kvm_test_arm64",
    tags = [
        "manual",
        "nogotsan",
    ],
    visibility = ["//test/rbe:__pkg__"],
)
