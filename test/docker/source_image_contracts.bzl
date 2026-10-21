# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Image obligations exported from the configured build graph for qualification."""

ImageArchiveInfo = provider(
    doc = "An image archive and its configured producer identity.",
    fields = {
        "image": "Canonical image name, architecture and declared archive file.",
    },
)

ImageSourceCommandInfo = provider(
    doc = "The source-owned selection of an image-building command.",
    fields = {
        "architecture": "Architecture of images built inside the command.",
        "contexts": "Configured archive containing this command's build contexts.",
        "image_class": "Canonical Make CPU or GPU image cohort.",
        "images": "Selected image names; empty selects the complete cohort.",
    },
)

ImageUsageInfo = provider(
    doc = "Source-owned import and staging roles for a configured CRI consumer.",
    fields = {
        "images": "Image names mapped to required import or staging-only usage.",
        "config": "Configured file that maps those names to archive runfiles.",
    },
)

_ImageInputsInfo = provider(
    doc = "The image obligations of a configured target and its dependencies.",
    fields = {
        "images": "Transitive declared image archives, independent of execution results.",
        "source_commands": "Commands that build images during test execution.",
    },
)

def _declared_image_archive_impl(ctx):
    archive = ctx.file.archive
    dependency = ctx.attr.archive[DefaultInfo]
    inherited = ctx.attr.archive[ImageArchiveInfo].image if ImageArchiveInfo in ctx.attr.archive else None
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(ctx.attr.architecture, ctx.attr.architecture)
    if inherited != None and (inherited.name != ctx.attr.image or inherited.architecture != architecture):
        fail("archive aliases must preserve the declared image and architecture")
    origin = ctx.attr.origin or (inherited.origin if inherited != None else "source")
    layout = ctx.file.layout or (inherited.layout if inherited != None else None)
    pin = ctx.file.pin or (inherited.pin if inherited != None else None)
    if inherited != None and (origin != inherited.origin or layout != inherited.layout or pin != inherited.pin):
        fail("archive aliases must preserve the declared producer")
    if (origin == "source") != (layout == None):
        fail("native OCI and registry archives require their configured layout; source archives use their source action")
    if (origin == "registry") != (pin != None):
        fail("registry archives require the existing pull repository's digest file")
    runfiles = ctx.runfiles(files = [archive]).merge_all([
        files
        for files in [dependency.default_runfiles, dependency.data_runfiles]
        if files != None
    ])
    return [
        DefaultInfo(
            files = depset([archive]),
            runfiles = runfiles,
        ),
        ImageArchiveInfo(image = struct(
            name = ctx.attr.image,
            architecture = architecture,
            archive = archive,
            origin = origin,
            layout = layout,
            usage = "docker-load",
            config = None,
            pin = pin,
        )),
    ]

declared_image_archive = rule(
    implementation = _declared_image_archive_impl,
    doc = "Preserves a Docker archive's image identity for source qualification.",
    attrs = {
        "archive": attr.label(mandatory = True, allow_single_file = True),
        "architecture": attr.string(mandatory = True, values = ["amd64", "arm64", "x86_64", "aarch64"]),
        "image": attr.string(mandatory = True),
        "origin": attr.string(values = ["", "source", "native-oci", "registry"], doc = "Producer kind; empty inherits the archive declaration."),
        "layout": attr.label(allow_single_file = True, doc = "Configured OCI layout for a native assembly or registry dependency."),
        "pin": attr.label(allow_single_file = True, doc = "The registry pull repository's existing digest target."),
    },
)

def _owner_label(label):
    # BEP omits the canonical main-repository prefix from owner labels.
    label = str(label)

    # This recognizes Bazel's output; it does not name a repository dependency.
    # buildifier: disable=canonical-repository
    return label[2:] if label.startswith("@@//") else label

def _image_record(image, pins, indexes):
    return {
        "name": image.name,
        "architecture": image.architecture,
        "archive": image.archive.path,
        "origin": image.origin,
        "layout": image.layout.path if image.layout != None else "",
        "usage": image.usage,
        "config": image.config.path if image.config != None else "",
        "pin": pins[image.pin].path if image.pin != None else "",
        "index": indexes[image.layout].path if image.origin == "registry" else "",
    }

def _source_image_contracts_impl(target, ctx):
    if ImageArchiveInfo in target:
        # The archive's build can itself consume images. Those are producer
        # inputs, not additional archives the test is expected to load.
        images = depset([target[ImageArchiveInfo].image])
        commands = depset()
    elif ImageSourceCommandInfo in target:
        # Context archives can contain images outside this command's selected
        # cohort. Its own selection must be reconciled with its executed loads.
        command = target[ImageSourceCommandInfo]
        images = depset()
        commands = depset([struct(
            owner = _owner_label(ctx.label),
            architecture = command.architecture,
            image_class = command.image_class,
            images = tuple(command.images),
            contexts = command.contexts,
            context_images = tuple(depset(transitive = [
                dependency[_ImageInputsInfo].images
                for dependency in ctx.rule.attr.contexts
            ]).to_list()),
        )])
    else:
        transitive = []
        transitive_commands = []
        if ctx.rule:
            for name in dir(ctx.rule.attr):
                value = getattr(ctx.rule.attr, name)
                if type(value) == "dict":
                    # CRI configurations use label-keyed string dictionaries.
                    dependencies = value.keys() + value.values()
                else:
                    dependencies = value if type(value) == "list" else [value]
                for dependency in dependencies:
                    if type(dependency) == "Target" and _ImageInputsInfo in dependency:
                        transitive.append(dependency[_ImageInputsInfo].images)
                        transitive_commands.append(dependency[_ImageInputsInfo].source_commands)
        images = depset(transitive = transitive)
        commands = depset(transitive = transitive_commands)
    if ImageUsageInfo in target:
        usage = target[ImageUsageInfo]
        if sorted(usage.images.keys()) != sorted([image.name for image in images.to_list()]):
            fail("image usage must cover exactly the configured archive inputs")
        images = depset([struct(
            name = image.name,
            architecture = image.architecture,
            archive = image.archive,
            origin = image.origin,
            layout = image.layout,
            usage = usage.images[image.name],
            config = usage.config,
            pin = image.pin,
        ) for image in images.to_list()])
    providers = [_ImageInputsInfo(images = images, source_commands = commands)]
    if ctx.rule:
        pins = {}
        indexes = {}
        inventory = images.to_list() + [image for command in commands.to_list() for image in command.context_images]
        for image in inventory:
            if image.pin != None and image.pin not in pins:
                pin = ctx.actions.declare_file(ctx.label.name + ".source-image-pin-" + str(len(pins)))
                ctx.actions.expand_template(template = image.pin, output = pin, substitutions = {})
                pins[image.pin] = pin
            if image.origin == "registry" and image.layout not in indexes:
                index = ctx.actions.declare_file(ctx.label.name + ".source-image-index-" + str(len(indexes)) + ".json")
                ctx.actions.run_shell(
                    inputs = [image.layout],
                    outputs = [index],
                    arguments = [image.layout.path, index.path],
                    command = 'cp "$1/index.json" "$2"',
                    mnemonic = "RegistryImageIndex",
                )
                indexes[image.layout] = index
        output = ctx.actions.declare_file(ctx.label.name + ".source-images.json")
        ctx.actions.write(output, json.encode({
            "schema_version": 1,
            "owner": _owner_label(ctx.label),
            "images": [_image_record(image, pins, indexes) for image in images.to_list()],
            # These commands build and load their own images, so the absence
            # of archive inputs does not mean there is nothing to qualify.
            "source_commands": [{
                "owner": command.owner,
                "architecture": command.architecture,
                "image_class": command.image_class,
                "images": command.images,
                "contexts": command.contexts.path,
                "context_images": [_image_record(image, pins, indexes) for image in command.context_images],
            } for command in commands.to_list()],
        }))
        providers.append(OutputGroupInfo(source_image_contracts = depset([output] + pins.values() + indexes.values())))
    return providers

source_image_contracts = aspect(
    implementation = _source_image_contracts_impl,
    # Rule-specific attribute lists can silently drop a new wrapper or a
    # label-keyed archive mapping. The declared-archive boundary above stops
    # producer inputs from becoming additional consumer image obligations.
    attr_aspects = ["*"],
    doc = "Exports each configured root's complete declared image obligations, including empty sets.",
)
