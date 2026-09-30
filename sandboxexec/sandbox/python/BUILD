load("@bazel_skylib//rules:common_settings.bzl", "string_flag")
load("@bazel_skylib//rules:native_binary.bzl", "native_test")
load("@python_install_smoke_deps//:requirements.bzl", "requirement")
load("@rules_python//python:py_binary.bzl", "py_binary")
load("@rules_python//python:py_library.bzl", "py_library")
load("@rules_python//python:py_test.bzl", "py_test")
load("//tools/python_distribution:defs.bzl", "python_distribution")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

py_library(
    name = "sandbox_py",
    srcs = [
        "gvisor/__init__.py",
        "gvisor/sandbox.py",
    ],
    imports = ["."],
    visibility = ["//:__subpackages__"],
)

exports_files(
    ["gvisor/sandbox.py"],
    visibility = ["//:__subpackages__"],
)

string_flag(
    name = "release_version",
    build_setting_default = "",
)

python_distribution(
    name = "dist",
    srcs = ["README.md"] + glob(["gvisor/*.py"]),
    pyproject = "pyproject.toml",
    version = ":release_version",
)

py_test(
    name = "sandbox_py_test",
    srcs = ["tests/sandbox_test.py"],
    data = ["//:release"],
    main = "tests/sandbox_test.py",
    tags = ["nogotsan"],
    deps = [
        ":sandbox_py",
    ],
)

# Fork-only installed-wheel smoke; the importer has no source-package deps.
py_binary(
    name = "install_smoke_installer",
    main_module = "installer",
    deps = [requirement("installer")],
)

py_binary(
    name = "install_smoke_importer",
    interpreter_args = ["-v"],
    main_module = "gvisor.sandbox",
)

native_test(
    name = "installed_import_test",
    src = "installed_import_smoke.sh",
    args = [
        "$(rootpath :install_smoke_installer)",
        "$(rootpath :install_smoke_importer)",
        "$(rootpath :dist)",
    ],
    data = [
        ":dist",
        ":install_smoke_importer",
        ":install_smoke_installer",
    ],
    tags = ["manual"],
)
