load("@rules_cc//cc:cc_library.bzl", "cc_library")
load(":probe.bzl", "exec_tool")

exports_files(["instrumentor-template.patch"])

cc_library(
    name = "embedded",
    srcs = ["embedded.cc"],
    additional_compiler_inputs = ["@llvm-project//llvm:instrumentor_variables_gen"],
    hdrs = ["@llvm-project//llvm:instrumentor_variables_gen"],
    linkstatic = True,
    copts = ["-include", "$(execpath @llvm-project//llvm:instrumentor_variables_gen)"],
)

exec_tool(name = "clang_tool", tool = "@llvm-project//clang:clang-tblgen")
exec_tool(name = "llvm_tool", tool = "@llvm-project//llvm:llvm-min-tblgen")
