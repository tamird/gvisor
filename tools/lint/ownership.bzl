"""Package ownership prototype for declared source formatting."""

def format_sources():
    """Declare local sources and require matching declarations in child packages."""
    if native.existing_rule("format_sources") != None:
        return
    package = native.package_name()
    parts = package.split("/") if package else []
    children = native.subpackages(
        include = ["**"],
        exclude = ["bazel-*", "bazel-*/**"],
        allow_empty = True,
    )
    native.filegroup(
        name = "format_sources",
        srcs = native.glob(
            ["**/*.go", "**/*.bzl", "BUILD", "BUILD.bazel", "MODULE.bazel", "WORKSPACE"],
            exclude = ["bazel-*/**"],
            allow_empty = True,
        ) + [
            "//{}:format_sources".format("/".join([part for part in [package, child] if part]))
            for child in children
        ],
        visibility = [
            "//{}:__pkg__".format("/".join(parts[:i]))
            for i in range(len(parts))
        ],
    )
