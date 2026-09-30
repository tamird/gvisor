"""Declared inputs and execution requirements for the maintained Docker suites."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@rules_oci//oci:defs.bzl", "oci_load")
load("//tools:arch.bzl", "select_arch")
load("//tools:defs.bzl", "go_test")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")
load(":config.bzl", "AMD64_IMAGES", "AMD64_RUNTIME_IMAGES", "COHORT_IMAGES")

def _image_name(image):
    return image.replace("/", "_").replace("-", "_")

def docker_image_archive(name, image, architecture):
    """Declares a Docker archive of an existing image pinned in MODULE.

    Args:
      name: Archive target name; the tarball is exposed as name + "_tar".
      image: Image name relative to gvisor.dev/images.
      architecture: Architecture of the declared image.
    """
    repository = "docker_image_" + _image_name(image) + "_" + architecture
    oci_load(
        name = name,
        image = "@" + repository,
        repo_tags = ["gvisor.dev/images/" + image + ":latest"],
        tags = ["manual"],
    )
    native.filegroup(
        name = name + "_tar",
        srcs = [":" + name],
        output_group = "tarball",
        tags = ["manual"],
    )

def docker_image_archives(name, extra_images = []):
    """Declares Docker-format archives of the suite images pinned in MODULE.

    Args:
      name: Prefix for the archive targets.
      extra_images: Archives consumed directly without loading them into Docker.
    """
    images = {image: True for cohort in COHORT_IMAGES.values() for image in cohort}
    images.update({image: True for image in AMD64_RUNTIME_IMAGES})
    images.update({image: True for image in extra_images})
    for image in sorted(images):
        image_name = _image_name(image)
        architectures = ["amd64"] if image in AMD64_IMAGES else ["amd64", "arm64"]
        for arch in architectures:
            docker_image_archive(
                name = name + "_" + image_name + "_" + arch,
                image = image,
                architecture = arch,
            )

def _daemon_config_impl(ctx):
    runtime = ctx.executable.runtime
    output = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(output, json.encode({
        "runsc": runtime.short_path,
        "images": [archive.short_path for archive in ctx.files.images],
        "runtime_args": ctx.attr.runtime_args,
        "ipv6": ctx.attr.ipv6,
    }))
    runfiles = ctx.runfiles(files = [runtime] + ctx.files.images)
    runfiles = runfiles.merge(ctx.attr.runtime[DefaultInfo].default_runfiles)
    return [DefaultInfo(files = depset([output]), runfiles = runfiles)]

docker_daemon_config = rule(
    implementation = _daemon_config_impl,
    doc = "Declares the release, image archives and runtime arguments for an owned Docker daemon.",
    attrs = {
        "runtime": attr.label(default = "//:release", executable = True, cfg = "target"),
        "images": attr.label_list(allow_files = True),
        "runtime_args": attr.string_list(),
        "ipv6": attr.bool(doc = "Enable the default Docker bridge's IPv6 subnet."),
    },
)

def docker_test(name, cohort = None, data = [], args = [], owned_args = [], nogo = True, runtime_variants = None, ipv6 = False, **kwargs):
    """Runs an existing Go suite against installed or declared Docker inputs.

    Args:
      name: Existing test target name.
      cohort: Optional key in COHORT_IMAGES identifying owned image inputs.
      data: Other existing runtime inputs.
      args: Arguments shared by the installed and owned test entrypoints.
      owned_args: Additional arguments for owned test entrypoints only.
      nogo: Whether this target owns static analysis of the test sources.
      runtime_variants: Optional named runtime arguments, test_args, data and tags for owned actions.
      ipv6: Whether the owned daemon provides IPv6 on its default bridge.
      **kwargs: Remaining go_test arguments.
    """
    if cohort == None and (owned_args or runtime_variants != None or ipv6):
        fail("owned arguments, runtime variants and IPv6 require an image cohort")

    # Bazel's native local attribute is nonconfigurable. Keep the installed
    # entrypoint and generate owned variants from the same source/deps.
    # Only the installed target creates a Nogo target: analysis does not need
    # the owned daemon's runtime image archives.
    go_test(
        name = name,
        args = args,
        data = data,
        local = True,
        nogo = nogo,
        **kwargs
    )

    if cohort == None:
        return

    owned_docker_test(
        name = name,
        cohort = cohort,
        data = data,
        args = args + owned_args,
        runtime_variants = runtime_variants,
        ipv6 = ipv6,
        **kwargs
    )

def owned_docker_test(name, cohort = None, data = [], args = [], runtime_variants = None, ipv6 = False, memory = None, free_disk = None, **kwargs):
    """Runs existing Go sources with a declared Docker daemon and images.

    Unlike docker_test, this only declares owned actions; its caller owns the
    installed entrypoint and static analysis of the same sources.

    Args:
      name: Prefix for owned target names.
      cohort: Default COHORT_IMAGES key; individual variants may override it.
      data: Other existing runtime inputs.
      args: Arguments shared by all variants.
      runtime_variants: Optional runtime arguments, cohort, test_args, data and tags.
      ipv6: Whether the owned daemon provides IPv6 on its default bridge.
      memory: Optional test VM memory budget; defaults to the fixture's 4GB.
      free_disk: Optional test VM disk budget; defaults to the existing cohort budget.
      **kwargs: Remaining go_test arguments.
    """
    tests = []
    variants = runtime_variants if runtime_variants != None else [struct(name = "", args = [])]
    for variant in variants:
        variant_cohort = getattr(variant, "cohort", cohort)
        if variant_cohort not in COHORT_IMAGES:
            fail("unknown Docker image cohort: %s" % variant_cohort)
        images = sorted(COHORT_IMAGES[variant_cohort])
        amd64 = images + (AMD64_RUNTIME_IMAGES if variant_cohort == "runtime" else [])
        arm64 = [image for image in images if image not in AMD64_IMAGES]
        prefix = name + ("_" + variant.name if variant.name else "")
        config = prefix + "_docker_config"
        docker_daemon_config(
            name = config,
            testonly = True,
            runtime_args = variant.args,
            ipv6 = ipv6,
            images = select_arch(
                amd64 = ["//test/docker:images_" + _image_name(image) + "_amd64_tar" for image in amd64],
                arm64 = ["//test/docker:images_" + _image_name(image) + "_arm64_tar" for image in arm64],
            ),
            tags = ["manual"],
        )
        test = prefix + "_owned"
        owned_kwargs = dict(kwargs)
        owned_kwargs["tags"] = kwargs.get("tags", []) + getattr(variant, "tags", [])
        if arm64 != images:
            owned_kwargs["target_compatible_with"] = kwargs.get("target_compatible_with", []) + select_arch(
                amd64 = [],
                arm64 = ["@platforms//:incompatible"],
            )
        go_test(
            name = test,
            nogo = False,
            args = ["--docker_test_config=$(rootpath :" + config + ")"] + args + getattr(variant, "test_args", []),
            data = data + [":" + config] + getattr(variant, "data", []),
            rundir = ".",
            # Image layers and container writes use the explicitly sized root disk.
            exec_properties = docker_test_exec_properties(
                free_disk = free_disk if free_disk != None else ("30GB" if variant_cohort == "image" else "20GB"),
                memory = memory,
            ),
            **owned_kwargs
        )
        tests.append(test)
    if runtime_variants != None:
        native.test_suite(
            name = name + "_owned",
            tests = tests,
            tags = ["manual"],
            visibility = kwargs.get("visibility"),
        )

def _docker_command_test_impl(ctx):
    command = ctx.executable.command
    wrapper = ctx.executable._wrapper
    config = ctx.file.docker_config
    arguments = [
        "--docker_test_config=" + config.short_path,
        "--",
        command.short_path,
    ] + [ctx.expand_location(arg, targets = ctx.attr.data) for arg in ctx.attr.command_args]
    runner = ctx.actions.declare_file(ctx.label.name + "-runner")
    ctx.actions.write(runner, "\n".join([
        "#!/bin/bash",
        "exec %s %s \"$@\"" % (shell.quote(wrapper.short_path), " ".join([shell.quote(arg) for arg in arguments])),
        "",
    ]), is_executable = True)
    runfiles = ctx.runfiles(files = [command, wrapper, config] + ctx.files.data)
    for target in [ctx.attr.command, ctx.attr._wrapper, ctx.attr.docker_config] + ctx.attr.data:
        dependency_runfiles = target[DefaultInfo].default_runfiles
        if dependency_runfiles:
            runfiles = runfiles.merge(dependency_runfiles)
    return [DefaultInfo(executable = runner, runfiles = runfiles)]

docker_command_test = rule(
    implementation = _docker_command_test_impl,
    doc = "Runs a declared command with the shared private Docker test daemon.",
    test = True,
    attrs = {
        "command": attr.label(mandatory = True, executable = True, cfg = "target", allow_files = True),
        "command_args": attr.string_list(doc = "Command arguments, with location expansion against data."),
        "data": attr.label_list(allow_files = True),
        "docker_config": attr.label(mandatory = True, allow_single_file = True),
        "_wrapper": attr.label(default = "//test/docker/runner", executable = True, cfg = "target"),
    },
)
