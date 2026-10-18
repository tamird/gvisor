load("@rules_pkg//pkg:mappings.bzl", "pkg_attributes", "pkg_files", "strip_prefix")
load("@rules_python//python:defs.bzl", "py_binary")

package(default_visibility = ["//visibility:public"])

exports_files(glob(["all_models/inflight_batcher_llm/**"]) + [
    "tools/fill_template.py",
    "LICENSE",
])

py_binary(
    name = "fill_template",
    srcs = ["tools/fill_template.py"],
    main = "tools/fill_template.py",
)

# Preserve the executable bits from the pinned Git tree. Git checkout uses
# 0644/0755; the codeload tar's 0664/0775 modes are archive defaults.
_EXECUTABLES = [
    "all_models/inflight_batcher_llm/preprocessing/1/model.py",
    "all_models/inflight_batcher_llm/tensorrt_llm/1/model.py",
]

pkg_files(
    name = "model_files",
    srcs = glob(
        ["all_models/inflight_batcher_llm/**"],
        exclude = _EXECUTABLES + ["all_models/inflight_batcher_llm/*/config.pbtxt"],
    ),
    attributes = pkg_attributes(mode = "0644"),
    prefix = "models",
    strip_prefix = strip_prefix.from_pkg("all_models/inflight_batcher_llm"),
)

pkg_files(
    name = "executable_model_files",
    srcs = _EXECUTABLES,
    attributes = pkg_attributes(mode = "0755"),
    prefix = "models",
    strip_prefix = strip_prefix.from_pkg("all_models/inflight_batcher_llm"),
)
