"""Expose the existing LLVM CodeGen compile output selected by the parser."""

def _object(ctx):
    files = [
        f
        for f in ctx.attr.codegen[OutputGroupInfo].compilation_outputs.to_list()
        if f.basename in ["RDFGraph.o", "RDFGraph.pic.o"]
    ]
    if not files:
        fail("CodeGen did not declare an RDFGraph object")
    return [DefaultInfo(files = depset(files))]

rdf_graph_object = rule(
    implementation = _object,
    attrs = {"codegen": attr.label(default = "@llvm-project//llvm:CodeGen")},
)
