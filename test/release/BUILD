load("@rules_shell//shell:sh_test.bzl", "sh_test")

sh_test(
    name = "sdist_py37_install_qualification",
    size = "small",
    srcs = ["sdist_py37_qualification.sh"],
    args = [
        "$(rootpath //sandboxexec/sandbox/python:dist)",
        "$(rootpath @sdist_py37_flit_core//file)",
        "$(rootpath @sdist_py37_setuptools//file)",
        "$(rootpath @sdist_py37_wheel//file)",
    ],
    data = [
        "//sandboxexec/sandbox/python:dist",
        "@sdist_py37_flit_core//file",
        "@sdist_py37_setuptools//file",
        "@sdist_py37_wheel//file",
    ],
    exec_properties = {
        "Arch": "amd64",
        "container-image": "docker://mirror.gcr.io/library/python@sha256:ea8897698c0955ba96144bd2b7310ef7884ccce4db7a1f97ffc21fb8b89d1673",
    },
)
