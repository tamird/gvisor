load("//test/runner:defs.bzl", "ordinary_syscall_suites", "syscall_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# The guest scripts use iproute2 and OpenBSD netcat.
_SYSCALL_IMAGE = "docker://docker.io/istio/base@sha256:cab6852ff5ae39349136f41af6ee892a228c8fb9634ec25e7550bd9b517a7a93"

ordinary_syscall_suites()

syscall_test(
    size = "small",
    container = True,
    overlay = True,
    save = False,
    syscall_image = _SYSCALL_IMAGE,
    test = "//test/rtnetlink/linux:veth_test",
    use_tmpfs = True,
)

syscall_test(
    size = "small",
    container = True,
    overlay = True,
    save = False,
    syscall_image = _SYSCALL_IMAGE,
    test = "//test/rtnetlink/linux:bridge_test",
    use_tmpfs = True,
)

syscall_test(
    size = "small",
    container = True,
    overlay = True,
    save = False,
    syscall_image = _SYSCALL_IMAGE,
    test = "//test/rtnetlink/linux:setlink_test",
    use_tmpfs = True,
)

syscall_test(
    size = "small",
    container = True,
    overlay = True,
    save = False,
    syscall_image = _SYSCALL_IMAGE,
    test = "//test/rtnetlink/linux:route_test",
    use_tmpfs = True,
)
