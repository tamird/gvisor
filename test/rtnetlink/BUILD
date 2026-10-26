load("//test/runner:defs.bzl", "syscall_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

syscall_test(
    size = "small",
    container = True,
    network_tools = True,
    overlay = True,
    save = False,
    # Emulated boot and the ip subprocesses can exhaust the small-test budget.
    tcg_timeout = "moderate",
    test = "//test/rtnetlink/linux:veth_test",
    use_tmpfs = True,
)

syscall_test(
    size = "small",
    container = True,
    network_tools = True,
    overlay = True,
    save = False,
    test = "//test/rtnetlink/linux:bridge_test",
    use_tmpfs = True,
)

syscall_test(
    size = "small",
    container = True,
    network_tools = True,
    overlay = True,
    save = False,
    test = "//test/rtnetlink/linux:setlink_test",
    use_tmpfs = True,
)

syscall_test(
    size = "small",
    container = True,
    network_tools = True,
    overlay = True,
    save = False,
    # Emulated boot and the ip subprocesses can exhaust the small-test budget.
    tcg_timeout = "moderate",
    test = "//test/rtnetlink/linux:route_test",
    use_tmpfs = True,
)
