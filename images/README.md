# Container Images

This directory contains all images used by tests.

Note that all these images must be pushed to the testing project hosted on
[Google Container Registry][gcr]. This will happen automatically as part of
continuous integration. This will speed up loading as images will not need to be
built from scratch for each test run.

Image tooling is accessible via `make`, specifically via `tools/images.mk`.

## Why make?

Make is used because it can bootstrap the `default` image, which contains
`bazel` and all other parts of the toolchain.

## Listing images

To list all images, use `make list-all-images` from the top-level directory.

## Loading and referencing images

To build a specific image, use `make load-<image>` from the top-level directory.
This will ensure that an image `gvisor.dev/images/<image>:latest` is available.

Images should always be referred to via the `gvisor.dev/images` canonical path.
This tag exists only locally, but serves to decouple tests from the underlying
image infrastructure.

The continuous integration system can either take fine-grained dependencies on
single images via individual `load` targets, or pull all images via a single
`load-all-test-images` invocation.

## CUDA image helpers

Bazel builds the CUDA sample runners with the repository Go SDK and CGO
disabled, and packages them with the image sources in
`//images:source_contexts`. The CUDA `make` image commands stage that archive
before computing the image hash, so changes to a helper or the SDK invalidate
the image cache. The selected architecture controls both the helper binaries and
the Docker build.

Docker still builds the separate ASCII image converter using `golang:1.25`.

## Declared image archives

The Ollama client and benchmark profile-helper images are defined in their
BUILD files using `rules_oci`. They combine Bazel-built static programs with
pinned Alpine and Ubuntu bases for AMD64 or ARM64. The profile helper keeps
Ubuntu because its Kubernetes caller also uses GNU coreutils. Building these
images does not start a Docker daemon.

Both the runtime tests and Make consume these images. The Make commands stage
each `make_context` archive and hash the complete image to derive its cache tag.
Rebuilding loads that archive before applying the local and registry tags;
cached pull and manifest checks retain their existing behavior. The archive
supplies the canonical `gvisor.dev/images/<path>:latest` tag, with custom Make
prefixes applied after loading. CPU and GPU image checks acquire only their
respective native image archive; individual Dockerfile builds acquire neither.

The other source-image actions reuse the Dockerfiles and Make's pull/rebuild
behavior. Their Linux execution worker must supply the Docker CLI and a
running daemon. The RBE configuration requests BuildBuddy's managed daemon;
a local Linux invocation requires an existing daemon. Runtime tests keep
their separate private daemon so they can select runsc and isolate test state.

## Adding new images

Images can be assembled with `rules_img`; see `basic/BUILD` for examples. Group
related images in one Bazel package. Declare immutable base images in
`MODULE.bazel` and expose each image with `image_outputs` from
`//images:img.bzl`. Its `image_name` preserves the image's test-facing name
independently of the Bazel package. Add the image's Make name and context target
to `BAZEL_IMAGE_CONTEXTS` in `tools/images.mk`. Make hashes the assembled
archive, including its transitive inputs, before checking the image cache. The
image can also be built directly with Bazel, without a Docker daemon:

```sh
bazel build --config=x86_64 //images/basic:tmpfile_archive
```

For a Docker build, create a directory under `images` containing a Dockerfile
and any other files that the image requires. You may choose to add to an
existing subdirectory if applicable, or create a new one.

All images will be tagged and memoized using a hash of the directory contents.
As a result, every image should be made completely reproducible if possible.
This means using fixed tags and fixed versions whenever feasible.

Note that images should also be made architecture-independent if possible. The
build scripts will handle loading the appropriate architecture onto the machine
and tagging it with the single canonical tag.

Add a `load-<image>` dependency in the Makefile if the image is required for a
particular set of tests. This target will pull the tag from the image repository
if available.

## Building and pushing images

All images can be built manually by running `build-<image>` and pushed using
`push-<image>`. Note that you can also use `push-all-images`. Note that pushing
will require appropriate permissions in the project.

The continuous integration system can either take fine-grained dependencies on
individual `push` targets, or ensure all images are up-to-date with a single
`push-all-images` invocation.

## Multi-Arch images

By default, the image is built for host architecture. Cross-building can be
achieved by specifying `ARCH` variable to make. For example:

```
$ make ARCH=aarch64 rebuild-default
```
