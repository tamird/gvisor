"""Defines a rule for syscall test targets."""

load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")
load("//test/rbe/tcg:defs.bzl", "amd64_kvm_test", "arm64_tcg_test")
load("//tools:defs.bzl", "default_platform", "platform_capabilities", "platforms", "save_restore_platforms", "syscall_test_exec_properties")
load("//tools/bazeldefs:test_architectures.bzl", "host_test_requirement_tags", "test_architecture_tags", "test_architecture_variants", "with_test_architecture")
load(":runner_test.bzl", _runner_test_rule = "runner_test")

# Maps platform names to a GVISOR_PLATFORM_SUPPORT environment variable consumed by platform_util.cc
_platform_support_env_vars = {
    platform: ",".join(sorted([
        ("%s:%s" % (capability, "TRUE" if supported else "FALSE"))
        for capability, supported in support.items()
    ]))
    for platform, support in platform_capabilities.items()
}

# Keep the public syscall compiler policy on this graph, rather than imposing
# it on unit and release targets in the same invocation.
def _runner_test_variant(runtime_coverage = False):
    configured = with_cfg(_runner_test_rule, extra_providers = [testing.ExecutionInfo]).extend("cxxopt", ["-Werror"])
    if runtime_coverage:
        configured = configured.set(Label("//tools:runtime_coverage"), True)
    return configured.build()

# with_cfg requires the returned transition rule to be exported at module scope.
# buildifier: disable=unused-variable
_runner_test, _runner_compilation_transition = _runner_test_variant()

# buildifier: disable=unused-variable
_coverage_runner_test, _runner_coverage_transition = _runner_test_variant(runtime_coverage = True)

def _compile_runner_test(compile_exec_compatible_with, runtime_coverage = False, **kwargs):
    kwargs["exec_compatible_with"] = compile_exec_compatible_with
    test_rule = _coverage_runner_test if runtime_coverage else _runner_test
    test_rule(**kwargs)

runner_amd64_test, _runner_amd64_transition = with_test_architecture(_compile_runner_test, "amd64", extra_providers = [testing.ExecutionInfo]).build()
runner_arm64_test, _runner_arm64_transition = with_test_architecture(_compile_runner_test, "arm64", extra_providers = [testing.ExecutionInfo]).build()
runner_arm64_64k_test, _runner_arm64_64k_transition = with_test_architecture(_compile_runner_test, "arm64", extra_providers = [testing.ExecutionInfo]).set(Label("//tools/bazeldefs:page_size"), "64k").build()

def _syscall_test(
        test,
        platform,
        use_tmpfs,
        tags,
        debug,
        network = "none",
        external_network = False,
        limit_ephemeral_ports = False,
        file_access = "exclusive",
        overlay = False,
        add_host_uds = False,
        add_host_connector = False,
        add_host_fifo = False,
        add_host_tty = False,
        iouring = False,
        container = None,
        one_sandbox = True,
        fusefs = False,
        directfs = False,
        leak_check = False,
        save = False,
        save_resume = False,
        nftables = False,
        kvm_use_cpu_nums = True,
        in_sandbox_cgroup = "v1",
        network_tools = False,
        memory = None,
        requires_atime = False,
        tcg_timeout = None,
        tcg_shard_count = None,
        native_kernel_requirements = [],
        hostnet_kernel_requirements = [],
        native_rc_images = {},
        kcov = False,
        supported_platforms = None,
        supported_networks = None,
        **kwargs):
    runtime_coverage = kcov and platform != "native"
    if external_network or limit_ephemeral_ports:
        container = True
    if external_network:
        if platform != "native" and network == "none":
            network = "sandbox"

    # Filter the effective runtime before emitting any architecture or RC tests.
    if supported_platforms != None and platform not in supported_platforms:
        return 0
    if supported_networks != None and network not in supported_networks:
        return 0

    # Prepend "runsc" to non-native platform names.
    full_platform = platform if platform == "native" else "runsc_" + platform

    # Name the test appropriately.
    name = test.split(":")[1] + "_" + full_platform
    if file_access == "shared":
        name += "_shared"
    if overlay:
        name += "_overlay"
    if network != "none":
        name += "_" + network + "net"
    if fusefs:
        name += "_fuse"
    if directfs:
        name += "_directfs"
    if save:
        name += "_save"
    if save_resume:
        name += "_save_resume"
    if nftables:
        name += "_nftables"

    # Apply all tags.
    if tags == None:
        tags = []

    # Add the full_platform and file access in a tag to make it easier to run
    # all the tests on a specific flavor. Use --test_tag_filters=runsc_systrap,file_shared.
    tags = list(tags)
    tags += [full_platform, "file_" + file_access]
    if platform == "native":
        tags += host_test_requirement_tags(native_kernel_requirements)
    elif network == "host":
        tags += host_test_requirement_tags(hostnet_kernel_requirements)

    if save or save_resume:
        tags.append("allsave")
        if platform in save_restore_platforms:
            if save:
                tags.append("save_restore")
            if save_resume:
                tags.append("save_resume")

    # Hash this target into one of 15 buckets. This can be used to
    # randomly split targets between different workflows.
    hash15 = hash(native.package_name() + name) % 15
    tags.append("hash15:" + str(hash15))
    tags.append("hash15")

    # Disable off-host networking.
    tags.append("requires-net:loopback")
    tags.append("requires-net:ipv4")
    tags.append("block-network")

    # gotsan makes sense only if tests are running in gVisor.
    if platform == "native":
        tags.append("nogotsan")

    if container == None:
        # Containerize in the following cases:
        #  - "container" is explicitly specified as a tag
        #  - Running tests natively
        #  - Running tests with host networking
        container = "container" in tags or network == "host"

    if platform == "native":
        # The "native" platform supports everything.
        platform_support = ",".join(sorted([
            ("%s:TRUE" % key)
            for key in platform_capabilities[default_platform].keys()
        ]))
    else:
        platform_support = _platform_support_env_vars.get(platform, "")

    runner_args = [
        # Arguments are passed directly to runner binary.
        "--platform=" + platform,
        "--platform-support=" + platform_support,
        "--network=" + network,
        "--use-tmpfs=" + str(use_tmpfs),
        "--fusefs=" + str(fusefs),
        "--file-access=" + file_access,
        "--overlay=" + str(overlay),
        "--add-host-uds=" + str(add_host_uds),
        "--add-host-connector=" + str(add_host_connector),
        "--add-host-fifo=" + str(add_host_fifo),
        "--add-host-tty=" + str(add_host_tty),
        "--strace=" + str(debug),
        "--debug=" + str(debug),
        "--container=" + str(container),
        "--one-sandbox=" + str(one_sandbox),
        "--iouring=" + str(iouring),
        "--directfs=" + str(directfs),
        "--leak-check=" + str(leak_check),
        "--save=" + str(save),
        "--save-resume=" + str(save_resume),
        "--nftables=" + str(nftables),
        "--kvm-use-cpu-nums=" + str(kvm_use_cpu_nums),
    ]

    if external_network:
        runner_args.append("--external-network")

    if limit_ephemeral_ports:
        runner_args.append("--limit-ephemeral-ports")

    if runtime_coverage:
        # Reporting consumes the counters that the KCOV syscall tests need.
        runner_args.append("--coverage-report=false")

    if in_sandbox_cgroup:
        runner_args.append("--in-sandbox-cgroup=" + in_sandbox_cgroup)

    # Trace points are platform agnostic, so enable them for ptrace only.
    if platform == "ptrace":
        runner_args.append("--trace")

    # Preserve explicit caller properties, including configurable values.
    kwargs.setdefault("exec_properties", syscall_test_exec_properties(platform, network_tools, memory))

    # Match the public ARM64 64K lane: systrap without checkpoint variants.
    arm64_64k = platform == "systrap" and "allsave" not in tags
    if arm64_64k:
        tags += ["rbe-has-64k-arm64-variant", "rbe-has-64k-tcg-variant"]

    # The public ARM64 RC lane uses ptrace and systrap. Native variants also
    # let focused tests use kernel features absent from hosted ARM64 workers.
    arm64_rc = platform in ["native", "ptrace", "systrap"] and "allsave" not in tags
    if arm64_rc:
        tags.append("rbe-has-rc-tcg-variant")
    amd64_rc = platform in ["native", "ptrace", "systrap", "kvm"] and "allsave" not in tags
    if amd64_rc:
        tags.append("rbe-has-rc-kvm-variant")

    attributes = dict(kwargs)
    attributes.update(
        test = test,
        # Native always uses a host directory; FUSE variants use guest tmpfs.
        requires_atime = requires_atime and (platform == "native" or not use_tmpfs),
        runner_args = runner_args,
        tags = test_architecture_tags(["amd64", "arm64"], tags),
    )
    test_rule = _coverage_runner_test if runtime_coverage else _runner_test
    test_rule(name = name, **attributes)
    test_architecture_variants(
        name,
        ["amd64", "arm64"],
        {"amd64": runner_amd64_test, "arm64": runner_arm64_test},
        dict(attributes, compile_exec_compatible_with = attributes.get("exec_compatible_with", []), runtime_coverage = runtime_coverage),
    )

    # Emulation may need more wall time than the native payload. Keep that
    # allowance on the outer TCG tests, without changing native or KVM tests.
    tcg_attributes = {key: value for key, value in kwargs.items() if key in ["args", "size", "timeout", "shard_count", "flaky"]}

    # Avoid per-syscall trace formatting under software emulation. Keep debug
    # logging; later arguments can opt back into tracing for diagnosis.
    tcg_attributes["args"] = ["--strace=false"] + tcg_attributes.get("args", [])
    if tcg_timeout != None:
        tcg_attributes["timeout"] = tcg_timeout
    if tcg_shard_count != None:
        tcg_attributes["shard_count"] = tcg_shard_count
    if arm64_64k:
        test_architecture_variants(
            name + "_64k",
            ["arm64"],
            {"arm64": runner_arm64_64k_test},
            dict(attributes, compile_exec_compatible_with = attributes.get("exec_compatible_with", []), runtime_coverage = runtime_coverage),
        )
        arm64_tcg_test(
            name = name + "_64k_tcg",
            payload = ":" + name + "_64k_arm64",
            tags = attributes["tags"] + ["arm64-64k-tcg"],
            **tcg_attributes
        )

    rc_images = {
        "amd64": "//test/rbe/tcg:amd64_rc_guest",
        "arm64": "//test/rbe/tcg:rc_guest",
    }
    if platform == "native":
        for architecture, image in native_rc_images.items():
            if architecture not in rc_images:
                fail("Unsupported native RC guest architecture: " + architecture)
            rc_images[architecture] = image

    if arm64_rc:
        arm64_tcg_test(
            name = name + "_rc_tcg",
            payload = ":" + name + "_arm64",
            image = rc_images["arm64"],
            tags = attributes["tags"] + ["arm64-rc-tcg"],
            **tcg_attributes
        )

    if amd64_rc:
        amd64_kvm_test(
            name = name + "_rc_kvm",
            payload = ":" + name + "_amd64",
            image = rc_images["amd64"],
            tags = attributes["tags"] + ["amd64-rc-kvm"],
            **{key: value for key, value in kwargs.items() if key in ["args", "size", "timeout", "shard_count", "flaky"]}
        )
    return 1

def all_platforms():
    """All platforms returns a list of all platforms."""
    available = dict(platforms.items())
    available[default_platform] = platforms.get(default_platform, [])
    return available.items()

def syscall_test_variants(
        test,
        use_tmpfs = False,
        add_fusefs = False,
        add_overlay = False,
        add_host_uds = False,
        add_host_connector = False,
        add_host_fifo = False,
        add_host_tty = False,
        add_hostinet = False,
        add_directfs = True,
        one_sandbox = True,
        iouring = False,
        allow_native = True,  # buildifier: disable=unused-variable
        leak_check = True,
        debug = True,
        container = None,
        tags = None,
        save = False,
        save_resume = False,
        size = "medium",
        timeout = None,
        overlay = False,
        nftables = False,
        kvm_use_cpu_nums = False,
        in_sandbox_cgroup = "v1",
        network_tools = False,
        **kwargs):
    """Generates syscall tests for all variants.

    Args:
      test: the test target.
      use_tmpfs: use tmpfs in the defined tests.
      add_fusefs: add a fusefs test.
      add_overlay: add an overlay test.
      add_host_uds: setup bound UDS on the host.
      add_host_connector: setup host threads to connect to bound UDS created by sandbox.
      add_host_fifo: setup FIFO files on the host.
      add_host_tty: setup host TTY at /dev/tty.
      add_hostinet: add a hostinet test.
      add_directfs: add a directfs test.
      one_sandbox: runs all test cases in one sandbox instance.
      iouring: enable IO_URING support.
      allow_native: generate a native test variant.
      debug: enable debug output.
      container: Run the test in a container. If None, determined from other information.
      tags: starting test tags.
      leak_check: enables leak check.
      save: save restore test.
      size: test size.
      timeout: timeout for the test.
      save_resume: save resume test.
      overlay: add overlayfs test variants.
      nftables: if nftables is true, enable nftables.
      kvm_use_cpu_nums: use cpu numbers in kvm platform.
      in_sandbox_cgroup: cgroup version to use inside the sandbox.
      network_tools: Supply iproute2 and OpenBSD netcat for remote execution.
      **kwargs: Additional test arguments; memory sets a remote memory budget
        and requires_atime enables host atime updates. tcg_timeout overrides
        the Bazel timeout category only for the outer ARM64 TCG tests. tcg_shard_count
        overrides the shard count only for those tests. native_kernel_requirements
        and hostnet_kernel_requirements declare host features needed only by the
        corresponding variants; the caller's worker policy chooses their placement.
        native_rc_images maps architectures to guest images for native RC tests
        requiring features absent from the default packaged kernels.
        kcov selects an instrumented gVisor runtime and leaves coverage counters
        available to the test instead of collecting a runtime coverage report.

    Returns:
      The number of runtime variants emitted.
    """
    generated = 0
    for platform, platform_tags in all_platforms():
        # Add directfs to the default platform variant.
        directfs = add_directfs and platform == default_platform
        generated += _syscall_test(
            test = test,
            platform = platform,
            use_tmpfs = use_tmpfs,
            add_host_uds = add_host_uds,
            add_host_connector = add_host_connector,
            add_host_fifo = add_host_fifo,
            add_host_tty = add_host_tty,
            tags = platform_tags + tags,
            iouring = iouring,
            directfs = directfs,
            debug = debug,
            container = container,
            one_sandbox = one_sandbox,
            leak_check = leak_check,
            save = save,
            save_resume = save_resume,
            size = size,
            timeout = timeout,
            overlay = overlay,
            nftables = nftables,
            kvm_use_cpu_nums = kvm_use_cpu_nums,
            in_sandbox_cgroup = in_sandbox_cgroup,
            network_tools = network_tools,
            **kwargs
        )

    if add_overlay:
        generated += _syscall_test(
            test = test,
            platform = default_platform,
            use_tmpfs = use_tmpfs,
            add_host_uds = add_host_uds,
            add_host_connector = add_host_connector,
            add_host_fifo = add_host_fifo,
            add_host_tty = add_host_tty,
            tags = platforms.get(default_platform, []) + tags,
            debug = debug,
            iouring = iouring,
            container = container,
            one_sandbox = one_sandbox,
            overlay = True,
            leak_check = leak_check,
            save = save,
            save_resume = save_resume,
            size = size,
            timeout = timeout,
            nftables = nftables,
            kvm_use_cpu_nums = kvm_use_cpu_nums,
            in_sandbox_cgroup = in_sandbox_cgroup,
            network_tools = network_tools,
            **kwargs
        )

    # Connected host sockets do not survive restore, so the save tests are
    # skipped and only the save_resume tests are generated.
    if add_hostinet and not save:
        generated += _syscall_test(
            test = test,
            platform = default_platform,
            use_tmpfs = use_tmpfs,
            network = "host",
            add_host_uds = add_host_uds,
            add_host_connector = add_host_connector,
            add_host_fifo = add_host_fifo,
            add_host_tty = add_host_tty,
            tags = platforms.get(default_platform, []) + tags,
            debug = debug,
            iouring = iouring,
            container = container,
            one_sandbox = one_sandbox,
            leak_check = leak_check,
            save = save,
            save_resume = save_resume,
            size = size,
            timeout = timeout,
            nftables = nftables,
            kvm_use_cpu_nums = kvm_use_cpu_nums,
            in_sandbox_cgroup = in_sandbox_cgroup,
            network_tools = network_tools,
            **kwargs
        )
    if not use_tmpfs:
        # Also test shared gofer access.
        generated += _syscall_test(
            test = test,
            platform = default_platform,
            use_tmpfs = use_tmpfs,
            add_host_uds = add_host_uds,
            add_host_connector = add_host_connector,
            add_host_fifo = add_host_fifo,
            add_host_tty = add_host_tty,
            tags = platforms.get(default_platform, []) + tags,
            iouring = iouring,
            debug = debug,
            container = container,
            one_sandbox = one_sandbox,
            file_access = "shared",
            leak_check = leak_check,
            save = save,
            save_resume = save_resume,
            size = size,
            timeout = timeout,
            nftables = nftables,
            kvm_use_cpu_nums = kvm_use_cpu_nums,
            in_sandbox_cgroup = in_sandbox_cgroup,
            network_tools = network_tools,
            **kwargs
        )
    if add_fusefs:
        generated += _syscall_test(
            test = test,
            platform = default_platform,
            use_tmpfs = True,
            fusefs = True,
            add_host_uds = add_host_uds,
            add_host_connector = add_host_connector,
            add_host_fifo = add_host_fifo,
            add_host_tty = add_host_tty,
            tags = platforms.get(default_platform, []) + tags,
            debug = debug,
            container = container,
            one_sandbox = one_sandbox,
            leak_check = leak_check,
            save = save,
            save_resume = save_resume,
            size = size,
            timeout = timeout,
            nftables = nftables,
            kvm_use_cpu_nums = kvm_use_cpu_nums,
            in_sandbox_cgroup = in_sandbox_cgroup,
            network_tools = network_tools,
            **kwargs
        )
    return generated

def syscall_test(
        test,
        use_tmpfs = False,
        add_fusefs = False,
        add_overlay = False,
        add_host_uds = False,
        add_host_connector = False,
        add_host_fifo = False,
        add_host_tty = False,
        add_hostinet = False,
        add_directfs = True,
        one_sandbox = True,
        iouring = False,
        allow_native = True,  # buildifier: disable=unused-variable
        leak_check = True,
        debug = None,
        container = None,
        tags = None,
        save = None,
        size = "medium",
        overlay = False,
        nftables = False,
        perf = False,
        kvm_use_cpu_nums = False,
        in_sandbox_cgroup = "v1",
        network_tools = False,
        supported_platforms = None,
        supported_networks = None,
        **kwargs):
    """syscall_test is a macro that will create targets for all platforms.

    Args:
      test: the test target.
      use_tmpfs: use tmpfs in the defined tests.
      add_fusefs: add a fusefs test.
      add_overlay: add an overlay test.
      add_host_uds: setup bound UDS on the host.
      add_host_connector: setup host threads to connect to bound UDS created by sandbox.
      add_host_fifo: setup FIFO files on the host.
      add_host_tty: setup host TTY at /dev/tty.
      add_hostinet: add a hostinet test.
      add_directfs: add a directfs test.
      one_sandbox: runs all test cases in one sandbox instance.
      iouring: enable IO_URING support.
      allow_native: generate a native test variant.
      debug: enable debug output.
      container: Run the test in a container. If None, determined from other information.
      tags: starting test tags.
      leak_check: enables leak check.
      save: enables save/restore and save/resume test variants.
      size: test size.
      overlay: add overlayfs test variants.
      nftables: if nftables is true, enable nftables.
      perf: test is a benchmark.
      kvm_use_cpu_nums: use cpu numbers in kvm platform.
      in_sandbox_cgroup: cgroup version to use inside the sandbox.
      network_tools: Supply iproute2 and OpenBSD netcat for remote execution.
      supported_platforms: Platforms on which the test is applicable, or None for
        all generated platforms. Native also requires allow_native.
      supported_networks: Applicable effective network modes (none, sandbox, host),
        or None for all generated modes. This does not add variants.
      **kwargs: Additional test arguments; memory sets a remote memory budget
        and requires_atime enables host atime updates. tcg_timeout overrides
        the Bazel timeout category only for the outer ARM64 TCG tests. tcg_shard_count
        overrides the shard count only for those tests. native_kernel_requirements
        and hostnet_kernel_requirements declare host features needed only by the
        corresponding variants; the caller's worker policy chooses their placement.
        native_rc_images maps architectures to guest images for native RC tests
        requiring features absent from the default packaged kernels.
        kcov selects an instrumented gVisor runtime and leaves coverage counters
        available to the test instead of collecting a runtime coverage report.
    """
    for name, selected, available in [
        ("supported_platforms", supported_platforms, [platform for platform, _ in all_platforms()] + ["native"]),
        ("supported_networks", supported_networks, ["none", "sandbox", "host"]),
    ]:
        if selected == None:
            continue
        if type(selected) != "list" or not selected:
            fail("%s must be None or a nonempty list" % name)
        for value in selected:
            if type(value) != "string" or value not in available:
                fail("Unknown %s value %r; expected one of %s" % (name, value, available))
    if not allow_native and supported_platforms != None and "native" in supported_platforms:
        fail("supported_platforms includes native but allow_native is False")
    kwargs["supported_platforms"] = supported_platforms
    kwargs["supported_networks"] = supported_networks

    if not tags:
        tags = []

    # Debug logs and save/save_resume test variants are disabled by default for
    # benchmarks and enabled by default otherwise.
    if debug == None:
        debug = not perf
    if save == None:
        # add_host_tty is not compatible with save/resume
        save = not perf and not add_host_tty
    if perf:
        tags.append("perf")

    generated = 0
    if allow_native:
        generated += _syscall_test(
            test = test,
            platform = "native",
            use_tmpfs = False,
            add_host_uds = add_host_uds,
            add_host_connector = add_host_connector,
            add_host_fifo = add_host_fifo,
            add_host_tty = add_host_tty,
            tags = tags,
            iouring = iouring,
            debug = debug,
            container = container,
            one_sandbox = one_sandbox,
            kvm_use_cpu_nums = kvm_use_cpu_nums,
            in_sandbox_cgroup = in_sandbox_cgroup,
            network_tools = network_tools,
            **kwargs
        )

    generated += syscall_test_variants(
        test,
        use_tmpfs,
        add_fusefs,
        add_overlay,
        add_host_uds,
        add_host_connector,
        add_host_fifo,
        add_host_tty,
        add_hostinet,
        add_directfs,
        one_sandbox,
        iouring,
        allow_native,
        leak_check,
        debug,
        container,
        tags,
        False,  # save, generate all tests without save variant.
        False,  # save_resume, generate all tests without save_resume variant.
        size,
        overlay = overlay,
        nftables = nftables,
        kvm_use_cpu_nums = kvm_use_cpu_nums,
        in_sandbox_cgroup = in_sandbox_cgroup,
        network_tools = network_tools,
        **kwargs
    )

    # Add save and save_resume variants to all other variants generated above.
    if save:
        # Disable go sanitizers for save tests.
        tags.append("nogotsan")
        generated += syscall_test_variants(
            test,
            use_tmpfs,
            add_fusefs,
            add_overlay,
            add_host_uds,
            add_host_connector,
            add_host_fifo,
            add_host_tty,
            add_hostinet,
            add_directfs,
            one_sandbox,
            iouring,
            allow_native,
            leak_check,
            debug,
            container,
            tags,
            True,  # save, generate all tests with save variant.
            False,  # save_resume, generate all tests without save_resume variant.
            "large",  # size, use size as large by default for all S/R tests.
            "long",  # timeout, use long timeout for S/R tests.
            nftables = nftables,
            kvm_use_cpu_nums = kvm_use_cpu_nums,
            in_sandbox_cgroup = in_sandbox_cgroup,
            network_tools = network_tools,
            **kwargs
        )

        # Add save resume variant to all other variants generated above.
        generated += syscall_test_variants(
            test,
            use_tmpfs,
            add_fusefs,
            add_overlay,
            add_host_uds,
            add_host_connector,
            add_host_fifo,
            add_host_tty,
            add_hostinet,
            add_directfs,
            one_sandbox,
            iouring,
            allow_native,
            leak_check,
            debug,
            container,
            tags,
            False,  # save, generate all tests without save variant.
            True,  # save_resume, generate all tests with save_resume variant.
            "large",  # size, use size as large by default for all S/R tests.
            "long",  # timeout, use long timeout for S/R tests.
            nftables = nftables,
            kvm_use_cpu_nums = kvm_use_cpu_nums,
            in_sandbox_cgroup = in_sandbox_cgroup,
            network_tools = network_tools,
            **kwargs
        )
    if not generated:
        fail("No syscall test variants are applicable for %s" % test)
