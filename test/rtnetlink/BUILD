load("//test/runner:defs.bzl", "arm64_syscall_suite", "syscall_test")
load("//tools:defs.bzl", "namespace_test_properties")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# The guest scripts use iproute2 and OpenBSD netcat.
_GUEST_EXEC_PROPERTIES = namespace_test_properties(
    "docker://docker.io/istio/base@sha256:cab6852ff5ae39349136f41af6ee892a228c8fb9634ec25e7550bd9b517a7a93",
)

arm64_syscall_suite(name = "arm64_syscalls")

syscall_test(
    size = "small",
    arm64_guest_exec_properties = _GUEST_EXEC_PROPERTIES,
    container = True,
    overlay = True,
    save = False,
    test = "//test/rtnetlink/linux:veth_test",
    use_tmpfs = True,
)

syscall_test(
    size = "small",
    arm64_guest_exec_properties = _GUEST_EXEC_PROPERTIES,
    container = True,
    overlay = True,
    save = False,
    test = "//test/rtnetlink/linux:bridge_test",
    use_tmpfs = True,
)

syscall_test(
    size = "small",
    arm64_guest_exec_properties = _GUEST_EXEC_PROPERTIES,
    container = True,
    overlay = True,
    save = False,
    test = "//test/rtnetlink/linux:setlink_test",
    use_tmpfs = True,
)

syscall_test(
    size = "small",
    arm64_guest_exec_properties = _GUEST_EXEC_PROPERTIES,
    container = True,
    overlay = True,
    save = False,
    test = "//test/rtnetlink/linux:route_test",
    use_tmpfs = True,
)
