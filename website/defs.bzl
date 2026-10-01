"""Wrappers for website documentation."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")
load("//tools:defs.bzl", "short_path")

# Keep the public website build's default stripping policy beside runtime tests.
website_artifact, _website_transition = with_cfg(native.filegroup).set("strip", "sometimes").build()

# The existing images/jekyll tool environment, published from source-hash tag
# 0366317e456d913b. Build and check scripts remain declared source inputs.
_JEKYLL_IMAGE = "docker://us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/jekyll_x86_64@sha256:97528c5497adaa2a507596d74cf89103aff46d44939ba4e3a93fd8aadec2f76e"

def _website_files_impl(ctx):
    output = ctx.actions.declare_file(ctx.label.name + ".tgz")
    build = shell.quote(ctx.file._build.path)
    checks = shell.quote(ctx.file._checks.path)
    if ctx.attr.remote:
        build_command = "/usr/jekyll/bin/entrypoint bash %s \"$T/input\" \"$T/output/_site\"" % build
        check_command = "/usr/jekyll/bin/entrypoint ruby %s \"$T/output/_site\"" % checks
    else:
        # Use the declared scripts even if the installed image embeds older ones.
        build_command = """docker run --rm -i --user "$(id -u):$(id -g)" \\
    -v "$T/input:/input" -v "$T/output/_site:/output" \\
    -v "$(readlink -m %s):/build.sh:ro" \\
    gvisor.dev/images/jekyll bash /build.sh /input /output""" % build
        check_command = """docker run --rm -i --user "$(id -u):$(id -g)" \\
    -v "$T/output/_site:/output" \\
    -v "$(readlink -m %s):/checks.rb:ro" \\
    gvisor.dev/images/jekyll ruby /checks.rb /output""" % checks

    command = [
        "set -euo pipefail",
        "T=$(mktemp -d)",
        "trap 'rm -rf \"$T\"' EXIT",
        "T=$(cd \"$T\" && pwd -P)",
        "mkdir -p \"$T/input\" \"$T/output/_site\"",
    ]
    command.extend([
        "tar -xf %s -C \"$T/input\"" % shell.quote(archive.path)
        for archive in ctx.files.archives
    ])
    command.extend([
        "find \"$T/input\" -type f -exec chmod u+rw {} \\;",
        build_command,
        "tar -xf %s -C \"$T/output/_site\"" % shell.quote(ctx.file.static.path),
        check_command,
        "cp %s \"$T/output/server\"" % shell.quote(ctx.file.server.path),
        "mkdir -p \"$T/output/etc/ssl\"",
        "cp %s \"$T/output/etc/ssl/cert.pem\"" % shell.quote(ctx.file.ca_certificate.path),
        "tar -zcf %s -C \"$T/output\" ." % shell.quote(output.path),
    ])
    ctx.actions.run_shell(
        inputs = ctx.files.archives + [ctx.file.static, ctx.file.server, ctx.file.ca_certificate, ctx.file._build, ctx.file._checks],
        outputs = [output],
        command = "\n".join(command),
        execution_requirements = {} if ctx.attr.remote else {"local": "1", "no-sandbox": "1"},
        mnemonic = "GvisorWebsiteFiles",
        progress_message = "Building and checking %s" % ctx.label,
        toolchain = None,
    )
    return [DefaultInfo(files = depset([output]))]

_website_files = rule(
    implementation = _website_files_impl,
    attrs = {
        "archives": attr.label_list(allow_files = True, doc = "Ordered Jekyll input archives."),
        "static": attr.label(allow_single_file = True, mandatory = True),
        "server": attr.label(allow_single_file = True, mandatory = True),
        "ca_certificate": attr.label(allow_single_file = True, mandatory = True),
        "remote": attr.bool(),
        "_build": attr.label(default = "//images/jekyll:build.sh", allow_single_file = True),
        "_checks": attr.label(default = "//images/jekyll:checks.rb", allow_single_file = True),
    },
)

def website_files(name, **kwargs):
    """Builds the website filesystem using installed Docker or remote Jekyll."""
    _website_files(
        name = name,
        remote = select({
            "//tools/bazeldefs:rbe": True,
            "//conditions:default": False,
        }),
        exec_properties = select({
            "//tools/bazeldefs:rbe": {
                "container-image": _JEKYLL_IMAGE,
                "workload-isolation-type": "oci",
            },
            "//conditions:default": {},
        }),
        # The published Jekyll tool image is Linux AMD64. This describes the
        # build tools, independently of the server's target architecture.
        exec_compatible_with = ["@platforms//os:linux", "@platforms//cpu:x86_64"],
        **kwargs
    )

# DocInfo is a provider which simple adds sufficient metadata to the source
# files (and additional data files) so that a jeyll header can be constructed
# dynamically. This is done the via BUILD system so that the plain
# documentation files can be viewable without non-compliant markdown headers.
DocInfo = provider(
    "Encapsulates information for a documentation page.",
    fields = [
        "layout",
        "description",
        "permalink",
        "category",
        "subcategory",
        "weight",
        "editpath",
        "authors",
        "include_in_menu",
        "body_classes",
    ],
)

def _doc_impl(ctx):
    return [
        DefaultInfo(
            files = depset(ctx.files.src + ctx.files.data),
        ),
        DocInfo(
            layout = ctx.attr.layout,
            description = ctx.attr.description,
            permalink = ctx.attr.permalink,
            category = ctx.attr.category,
            subcategory = ctx.attr.subcategory,
            weight = ctx.attr.weight,
            editpath = short_path(ctx.files.src[0].short_path),
            authors = ctx.attr.authors,
            include_in_menu = ctx.attr.include_in_menu,
            body_classes = ctx.attr.body_classes,
        ),
    ]

doc = rule(
    implementation = _doc_impl,
    doc = "Annotate a document for jekyll headers.",
    attrs = {
        "src": attr.label(
            doc = "The markdown source file.",
            mandatory = True,
            allow_single_file = True,
        ),
        "data": attr.label_list(
            doc = "Additional data files (e.g. images).",
            allow_files = True,
        ),
        "layout": attr.string(
            doc = "The document layout.",
            default = "docs",
        ),
        "description": attr.string(
            doc = "The document description.",
            default = "",
        ),
        "permalink": attr.string(
            doc = "The document permalink.",
            mandatory = True,
        ),
        "category": attr.string(
            doc = "The document category.",
            default = "",
        ),
        "subcategory": attr.string(
            doc = "The document subcategory.",
            default = "",
        ),
        "weight": attr.string(
            doc = "The document weight.",
            default = "50",
        ),
        "authors": attr.string_list(),
        "include_in_menu": attr.bool(
            doc = "Include document in the navigation menu.",
            default = True,
        ),
        "body_classes": attr.string_list(
            doc = "Classes to add to the body tag.",
            default = [],
        ),
    },
)

def _docs_impl(ctx):
    # Tarball is the actual output.
    tarball = ctx.actions.declare_file(ctx.label.name + ".tgz")

    # But we need an intermediate builder to translate the files.
    builder = ctx.actions.declare_file("%s-builder" % ctx.label.name)
    builder_content = [
        "#!/bin/bash",
        "set -euo pipefail",
        "declare -r T=$(mktemp -d)",
        "function cleanup {",
        "    rm -rf $T",
        "}",
        "trap cleanup EXIT",
    ]
    for dep in ctx.attr.deps:
        doc = dep[DocInfo]

        # Sanity check the permalink.
        if not doc.permalink.endswith("/"):
            fail("permalink %s for target %s should end with /" % (
                doc.permalink,
                ctx.label.name,
            ))

        # Construct the header.
        header = """\
description: {description}
permalink: {permalink}
category: {category}
subcategory: {subcategory}
weight: {weight}
editpath: {editpath}
authors: {authors}
layout: {layout}
include_in_menu: {include_in_menu}
body_classes: {body_classes}"""

        for f in dep.files.to_list():
            # Is this a markdown file? If not, then we ensure that it ends up
            # in the same path as the permalink for relative addressing.
            if not f.basename.endswith(".md"):
                builder_content.append("mkdir -p $T/%s" % doc.permalink)
                builder_content.append("cp %s $T/%s" % (f.path, doc.permalink))
                continue

            # Is this a post? If yes, then we must put this in the _posts
            # directory. This directory is treated specially with respect to
            # pagination and page generation.
            dest = f.short_path
            if doc.layout == "post":
                dest = "_posts/" + f.basename
            builder_content.append("echo Processing %s... >&2" % f.short_path)
            builder_content.append("mkdir -p $T/$(dirname %s)" % dest)

            # Construct the header dynamically. We include the title field from
            # the markdown itself, as this is the g3doc format required. The
            # title will be injected by the web layout however, so we don't
            # want this to appear in the document.
            args = dict([(k, getattr(doc, k)) for k in dir(doc)])
            builder_content.append("title=\"$(grep -E '^# ' %s | head -n 1 | cut -d'#' -f2- || true)\"" % f.path)
            builder_content.append("cat >$T/%s <<EOF" % dest)
            builder_content.append("---")
            builder_content.append("title: \"$title\"")
            builder_content.append("excerpt_separator: '<!--/excerpt-->'")
            builder_content.append(header.format(**args))
            builder_content.append("---")
            builder_content.append("EOF")

            # To generate the final page, we need to strip out the title (which
            # was pulled above to generate the annotation in the frontmatter,
            # and substitute the [TOC] tag with the {% toc %} plugin tag. Note
            # that the pipeline here is almost important, as the grep will
            # return non-zero if the file is empty, but we ignore that within
            # the pipeline.
            builder_content.append("awk '!found && /^# / {found=1; next} 1' %s | sed -e 's|^\\[TOC\\]$|- TOC\\n{:toc}|' >>$T/%s" %
                                   (f.path, dest))

    builder_content.append("declare -r filename=$(readlink -m %s)" % tarball.path)
    builder_content.append("(cd $T && tar -zcf \"${filename}\" .)\n")
    ctx.actions.write(builder, "\n".join(builder_content), is_executable = True)

    # Generate the tarball.
    ctx.actions.run(
        inputs = depset(ctx.files.deps),
        outputs = [tarball],
        mnemonic = "GvisorWebsiteDocs",
        progress_message = "Generating %s" % ctx.label,
        executable = builder,
        toolchain = None,
    )
    return [DefaultInfo(
        files = depset([tarball]),
    )]

docs = rule(
    implementation = _docs_impl,
    doc = "Construct a site tarball from doc dependencies.",
    attrs = {
        "deps": attr.label_list(
            doc = "All document dependencies.",
        ),
    },
)
