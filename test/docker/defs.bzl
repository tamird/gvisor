"""Declared inputs and execution requirements for the maintained Docker suites."""

load("@rules_oci//oci:defs.bzl", "oci_load")
load("//tools:arch.bzl", "select_arch")
load("//tools:defs.bzl", "go_test")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")
load(":config.bzl", "AMD64_RUNTIME_IMAGES", "COHORT_IMAGES")

def _image_name(image):
    return image.replace("/", "_").replace("-", "_")

def docker_image_archives(name):
    """Declares Docker-format archives of the suite images pinned in MODULE.

    Args:
      name: Prefix for the archive targets.
    """
    images = {image: True for cohort in COHORT_IMAGES.values() for image in cohort}
    images.update({image: True for image in AMD64_RUNTIME_IMAGES})
    for image in sorted(images):
        image_name = _image_name(image)
        architectures = ["amd64"] if image in AMD64_RUNTIME_IMAGES else ["amd64", "arm64"]
        for arch in architectures:
            repository = "docker_image_" + image_name + "_" + arch
            target = name + "_" + image_name + "_" + arch
            oci_load(
                name = target,
                image = "@" + repository,
                repo_tags = ["gvisor.dev/images/" + image + ":latest"],
                tags = ["manual"],
            )
            native.filegroup(
                name = target + "_tar",
                srcs = [":" + target],
                output_group = "tarball",
                tags = ["manual"],
            )

def _daemon_config_impl(ctx):
    runtime = ctx.executable.runtime
    output = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(output, json.encode({
        "runsc": runtime.short_path,
        "images": [archive.short_path for archive in ctx.files.images],
    }))
    runfiles = ctx.runfiles(files = [runtime] + ctx.files.images)
    runfiles = runfiles.merge(ctx.attr.runtime[DefaultInfo].default_runfiles)
    return [DefaultInfo(files = depset([output]), runfiles = runfiles)]

_daemon_config = rule(
    implementation = _daemon_config_impl,
    attrs = {
        "runtime": attr.label(default = "//:release", executable = True, cfg = "target"),
        "images": attr.label_list(allow_files = True),
    },
)

def docker_test(name, cohort, data = [], **kwargs):
    """Runs an existing Go suite against installed or declared Docker inputs.

    Args:
      name: Existing test target name.
      cohort: Key in COHORT_IMAGES identifying the suite's image inputs.
      data: Other existing runtime inputs.
      **kwargs: Remaining go_test arguments.
    """
    images = sorted(COHORT_IMAGES[cohort])
    amd64 = images + (AMD64_RUNTIME_IMAGES if cohort == "runtime" else [])
    config = name + "_docker_config"
    _daemon_config(
        name = config,
        testonly = True,
        images = select_arch(
            amd64 = ["//test/docker:images_" + _image_name(image) + "_amd64_tar" for image in amd64],
            arm64 = ["//test/docker:images_" + _image_name(image) + "_arm64_tar" for image in images],
        ),
        tags = ["manual"],
    )

    # Bazel's native local attribute is nonconfigurable. Keep the installed
    # entrypoint and generate the owned variant from the same source/deps.
    # Only the installed target creates a Nogo target: analysis does not need
    # the owned daemon's runtime image archives.
    go_test(
        name = name,
        data = data,
        local = True,
        **kwargs
    )
    go_test(
        name = name + "_owned",
        nogo = False,
        args = ["--docker_test_config=$(rootpath :" + config + ")"],
        data = data + [":" + config],
        rundir = ".",
        # Image layers and container writes use the explicitly sized root disk.
        exec_properties = docker_test_exec_properties(
            free_disk = "30GB" if cohort == "image" else "20GB",
        ),
        **kwargs
    )
