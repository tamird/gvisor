load(":probe.bzl", "probe")

platform(
    name = "scratch",
    constraint_values = ["@platforms//cpu:x86_64", "@platforms//os:linux"],
    exec_properties = {
        "OSFamily": "linux",
        "Arch": "amd64",
        "workload-isolation-type": "oci",
        "container-image": "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef",
    },
)

probe(
    name = "site_generator",
    driver = "runtime_probe.rb",
    checks = "checks.rb",
    runtime = glob(["tool_files/**"]),
    loader = "tool_files/lib/ld-musl-x86_64.so.1",
    gems = glob(["gems/*.gem"]),
)
