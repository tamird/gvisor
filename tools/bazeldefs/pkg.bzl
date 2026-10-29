"""Route only the package writers to scratch during release qualification."""

load("@rules_pkg//pkg:pkg.bzl", _pkg_deb = "pkg_deb", _pkg_tar = "pkg_tar")

_SCRATCH = {"container-image": "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"}

def pkg_deb(**kwargs):
    _pkg_deb(exec_properties = _SCRATCH, **kwargs)

def pkg_tar(**kwargs):
    _pkg_tar(exec_properties = _SCRATCH, **kwargs)
