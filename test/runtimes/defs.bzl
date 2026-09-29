"""Defines a rule for runtime test targets."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("//test/docker:defs.bzl", "docker_daemon_config", "docker_image_archive")
load("//tools:defs.bzl", "go_test", "local_test_tags")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")

_RUNTIME_MODES = {
    "directfs": True,
    "goferfs": False,
}

def _runtime_test_impl(ctx):
    # Construct arguments.
    args = [
        "--lang",
        ctx.attr.lang,
        "--image",
        ctx.attr.image,
        "--batch",
        str(ctx.attr.batch),
    ]
    files = [ctx.executable._runner, ctx.executable._proctor] + ctx.files._runsc
    if ctx.file.exclude_file:
        args += [
            "--exclude_file",
            ctx.file.exclude_file.short_path,
        ]
        files.append(ctx.file.exclude_file)
    if ctx.file.docker_config:
        args += ["--docker_test_config", ctx.file.docker_config.short_path]
        files.append(ctx.file.docker_config)

    # Build a runner.
    runner = ctx.actions.declare_file("%s-executer" % ctx.label.name)
    runner_content = "\n".join([
        "#!/bin/bash",
        "exec %s %s \"$@\"\n" % (
            shell.quote(ctx.executable._runner.short_path),
            " ".join([shell.quote(arg) for arg in args]),
        ),
    ])
    ctx.actions.write(runner, runner_content, is_executable = True)

    # Return the runner.
    runfiles = ctx.runfiles(files = files, collect_default = True, collect_data = True)
    if ctx.attr.docker_config:
        runfiles = runfiles.merge(ctx.attr.docker_config[DefaultInfo].default_runfiles)
    return [DefaultInfo(executable = runner, runfiles = runfiles)]

_runtime_test = rule(
    implementation = _runtime_test_impl,
    attrs = {
        "image": attr.string(
            mandatory = False,
        ),
        "lang": attr.string(
            mandatory = True,
        ),
        "exclude_file": attr.label(
            mandatory = False,
            allow_single_file = True,
        ),
        "batch": attr.int(
            default = 50,
            mandatory = False,
        ),
        "docker_config": attr.label(allow_single_file = True),
        "_runner": attr.label(
            default = "//test/runtimes/runner:runner",
            executable = True,
            cfg = "target",
        ),
        "_proctor": attr.label(
            default = "//test/runtimes/proctor:proctor",
            executable = True,
            cfg = "target",
        ),
        # Needed to invalidate the bazel cache in case of any code changes.
        "_runsc": attr.label(
            default = "//:release",
            cfg = "target",
        ),
    },
    test = True,
)

def runtime_test(name, partitions, memory = None, **kwargs):
    """Declares installed and owned entrypoints for a language runtime.

    Args:
      name: Existing runtime image and installed target name.
      partitions: Number of public CI partitions, each retaining its Bazel shards.
      memory: Optional memory budget for each owned test VM.
      **kwargs: Existing language, batch, exclusion and shard settings.
    """
    if partitions < 1:
        fail("runtime tests require at least one partition")
    _runtime_test(
        name = name,
        image = name,  # Resolved as images/runtimes/%s.
        tags = [
            "no-sandbox",
            "manual",
        ] + local_test_tags,
        **kwargs
    )

    archive = name + "_image_amd64"
    docker_image_archive(
        name = archive,
        image = "runtimes/" + name,
        architecture = "amd64",
    )
    for mode, directfs in _RUNTIME_MODES.items():
        prefix = name + "_" + mode
        config = prefix + "_docker_config"
        docker_daemon_config(
            name = config,
            testonly = True,
            images = [":" + archive + "_tar"],
            runtime_args = [
                "--platform=systrap",
                "--watchdog-action=panic",
                "--directfs=" + ("true" if directfs else "false"),
            ],
            tags = ["manual"],
        )
        tests = []

        # Bazel limits shard_count to 50. Keep public CI's outer partitions
        # as concrete tests and reuse its existing partition/shard selection.
        for partition in range(1, partitions + 1):
            test = prefix + "_" + str(partition) + "_owned"
            _runtime_test(
                name = test,
                image = name,
                docker_config = ":" + config,
                args = [
                    "--partition=" + str(partition),
                    "--total_partitions=" + str(partitions),
                ],
                exec_properties = docker_test_exec_properties(
                    free_disk = "20GB",
                    memory = memory,
                ),
                target_compatible_with = ["@platforms//cpu:x86_64"],
                tags = ["manual"],
                **kwargs
            )
            tests.append(test)
        native.test_suite(
            name = prefix + "_owned",
            tests = tests,
            tags = ["manual"],
        )

def runtime_tests(name, runtimes):
    """Declares the language owners and complete filesystem-mode suites.

    Args:
      name: Name suffix for the complete filesystem-mode suites.
      runtimes: Runtime names mapped to their existing runtime_test arguments.
    """
    for runtime, kwargs in runtimes.items():
        runtime_test(name = runtime, **kwargs)
    for mode in _RUNTIME_MODES:
        native.test_suite(
            name = mode + "_" + name,
            tests = [runtime + "_" + mode + "_owned" for runtime in runtimes],
            tags = ["manual"],
        )

def exclude_test(name, exclude_file):
    """Test that a exclude file parses correctly."""
    go_test(
        name = name + "_exclude_test",
        library = ":runner",
        srcs = ["exclude_test.go"],
        args = ["--exclude_file", "test/runtimes/" + exclude_file],
        data = [exclude_file],
    )
