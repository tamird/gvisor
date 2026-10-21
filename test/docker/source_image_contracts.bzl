"""Image obligations exported from the configured build graph for qualification."""

_ImageArchiveInfo = provider(fields = {
    "image": "Canonical image name, architecture and declared archive file.",
})

ImageSourceCommandInfo = provider(fields = {
    "architecture": "Architecture of images built inside the command.",
    "image_class": "Canonical Make CPU or GPU image cohort.",
    "images": "Selected image names; empty selects the complete cohort.",
})

_ImageInputsInfo = provider(fields = {
    "images": "Transitive declared image archives, independent of execution results.",
    "source_commands": "Commands that build images during test execution.",
})

def _declared_image_archive_impl(ctx):
    archive = ctx.file.archive
    dependency = ctx.attr.archive[DefaultInfo]
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
        _ImageArchiveInfo(image = struct(
            name = ctx.attr.image,
            architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(ctx.attr.architecture, ctx.attr.architecture),
            archive = archive,
        )),
    ]

declared_image_archive = rule(
    implementation = _declared_image_archive_impl,
    doc = "Preserves a Docker archive's image identity for source qualification.",
    attrs = {
        "archive": attr.label(mandatory = True, allow_single_file = True),
        "architecture": attr.string(mandatory = True, values = ["amd64", "arm64", "x86_64", "aarch64"]),
        "image": attr.string(mandatory = True),
    },
)

def _source_image_contracts_impl(target, ctx):
    if _ImageArchiveInfo in target:
        # The archive's build can itself consume images. Those are producer
        # inputs, not additional archives the test is expected to load.
        images = depset([target[_ImageArchiveInfo].image])
        commands = depset()
    elif ImageSourceCommandInfo in target:
        # Context archives can contain images outside this command's selected
        # cohort. Its own selection must be reconciled with its executed loads.
        command = target[ImageSourceCommandInfo]
        images = depset()
        commands = depset([struct(
            owner = str(ctx.label),
            architecture = command.architecture,
            image_class = command.image_class,
            images = tuple(command.images),
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
    providers = [_ImageInputsInfo(images = images, source_commands = commands)]
    if ctx.rule:
        output = ctx.actions.declare_file(ctx.label.name + ".source-images.json")
        ctx.actions.write(output, json.encode({
            "schema_version": 1,
            "owner": str(ctx.label),
            "images": [{
                "name": image.name,
                "architecture": image.architecture,
                "archive": image.archive.path,
            } for image in images.to_list()],
            # These commands build and load their own images, so the absence
            # of archive inputs does not mean there is nothing to qualify.
            "source_commands": [{
                "owner": command.owner,
                "architecture": command.architecture,
                "image_class": command.image_class,
                "images": command.images,
            } for command in commands.to_list()],
        }))
        providers.append(OutputGroupInfo(source_image_contracts = depset([output])))
    return providers

source_image_contracts = aspect(
    implementation = _source_image_contracts_impl,
    # Rule-specific attribute lists can silently drop a new wrapper or a
    # label-keyed archive mapping. The declared-archive boundary above stops
    # producer inputs from becoming additional consumer image obligations.
    attr_aspects = ["*"],
    doc = "Exports each configured root's complete declared image obligations, including empty sets.",
)
