"""Wrappers for website documentation."""

load("//tools:defs.bzl", "short_path")

# DocInfo is a provider which adds sufficient metadata to the source
# files (and additional data files) so that a Jekyll header can be constructed
# dynamically. This is done via the BUILD system so that the plain
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

SiteDocs = provider(fields = ["entries"])

def _docs_impl(ctx):
    entries = []
    sources = []
    for dep in ctx.attr.deps:
        doc = dep[DocInfo]
        if not doc.permalink.endswith("/"):
            fail("permalink %s for target %s should end with /" % (doc.permalink, ctx.label.name))
        sources.append(dep.files)
        for file in dep.files.to_list():
            entries.append(dict(
                file = file.path,
                short = file.short_path,
                layout = doc.layout,
                description = doc.description,
                permalink = doc.permalink,
                category = doc.category,
                subcategory = doc.subcategory,
                weight = doc.weight,
                editpath = doc.editpath,
                authors = doc.authors,
                include_in_menu = doc.include_in_menu,
                body_classes = doc.body_classes,
            ))
    return [
        DefaultInfo(files = depset(transitive = sources)),
        SiteDocs(entries = entries),
    ]

docs = rule(
    implementation = _docs_impl,
    doc = "Collect source files and their Jekyll front matter.",
    attrs = {"deps": attr.label_list(providers = [DocInfo])},
)
