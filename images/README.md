# Container Images

This directory contains all images used by tests.

Make targets in `tools/images.mk` build, load and publish images.
`REMOTE_IMAGE_PREFIX` selects the registry for cached images shared across
test runs.

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

## Go image helpers

Bazel builds the CUDA sample runners with the repository Go SDK and CGO
disabled, and packages them with the image sources in
`//images:source_contexts`. Their `make` image commands stage that archive before
computing the image hash, so changes to a helper or the SDK invalidate the image
cache. The selected architecture controls both the helper binaries and the
Docker build.

The ASCII image converter builds with `golang:1.25`.

## Bazel image targets

The Ollama client and benchmark profile helper have image targets in their
BUILD files. Bazel compiles each static Go program with the repository Go
toolchain and packages it as an executable layer. Both images use
`//images:helper_base`, which supplies CA trust and a writable `/tmp`.
The selected architecture controls the program and package inputs.

The profiling helper's Kubernetes caller invokes `mkdir`, `chmod`, and
`rm --one-file-system` directly. Its image adds these GNU commands, their
loader and shared libraries, and their license notices from snapshot-pinned
Ubuntu packages. The filesystem boundary prevents recursive removal from
crossing into nested mounts on the host.

Each package exposes an `image_archive` Docker tarball and a `load_image` target
that loads it into Docker. Its `make_context` target packages the tarball as
`images/<path>/image.tar`. Make stages this context before computing the cache
tag, so both the program and its base contribute to the tag. The Make rebuild
command loads the archive and applies the configured local and registry tags.

## Adding new images

For a Dockerfile image, create a directory under `images` containing its
Dockerfile and other required files. For a Bazel image, define its program and
image layers in a BUILD file, use `image_outputs` from `//images:oci.bzl` to
expose its archives, and add its Make target name to `BAZEL_IMAGES` in
`tools/images.mk`.

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

All images can be built manually by running `rebuild-<image>` and pushed using
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
